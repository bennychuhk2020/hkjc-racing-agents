from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright, Response

RAW_DEBUG_PATH = Path("data/raw_graphql_debug.json")
OUTPUT_DATA_PATH = Path("data/upcoming_races.json")

HKT = timezone(timedelta(hours=8))

DEFAULT_RACECARD_URL = (
    "https://racing.hkjc.com/zh-hk/local/information/racecard"
)
BET_WP_BASE_URL = "https://bet.hkjc.com/ch/racing/wp"


def build_target_url(
    race_date: str = "", course: str = "", race_no: int | None = None
) -> str:
  base_url = os.getenv("HKJC_TARGET_URL", DEFAULT_RACECARD_URL).strip()
  params = []
  if race_date:
    params.append(f"raceDate={race_date}")
  if course in {"ST", "HV"}:
    params.append(f"Racecourse={course}")
  if race_no is not None:
    params.append(f"RaceNo={race_no}")

  if not params:
    return base_url
  sep = "&" if "?" in base_url else "?"
  return f"{base_url}{sep}{'&'.join(params)}"


def safe_float(value: Any, default: float | None = None) -> float | None:
  if value is None:
    return default
  if isinstance(value, (int, float)):
    return float(value)
  text = str(value).strip()
  match = re.search(r"-?\d+(?:\.\d+)?", text.replace(",", ""))
  if not match:
    return default
  try:
    return float(match.group(0))
  except ValueError:
    return default


def safe_int(value: Any, default: int) -> int:
  val = safe_float(value, float(default))
  return int(round(val if val is not None else float(default)))


def clean_person_name(raw_text: str, default: str = "N/A") -> str:
  cleaned = re.sub(r"\s+", " ", str(raw_text or "")).strip()
  if not cleaned or cleaned in {"-", "--", "N/A"}:
    return default
  cleaned_no_paren = re.sub(r"\s*\([+-]?\d+\)\s*$", "", cleaned).strip()
  return cleaned_no_paren or cleaned


def infer_running_style(
    recent_runs: str, draw: int, distance_str: str
) -> str:
  """根據近績走勢、檔位與途程推算跑法類型。"""
  runs = [int(x) for x in re.findall(r"\b\d+\b", recent_runs.replace("/", " "))]
  dist = safe_int(distance_str, 1400)

  if runs:
    recent_3 = runs[:3]
    avg_pos = sum(recent_3) / len(recent_3)
    best_pos = min(recent_3)
    if best_pos == 1 and draw <= 4 and dist <= 1400:
      return "領放"
    if avg_pos <= 4.0:
      return "前置機動"
    if avg_pos <= 7.5:
      return "居中順走"
    return "後上"

  # 若無近績字串，按檔位與途程作合理戰術推算
  if draw <= 2 and dist <= 1200:
    return "領放"
  if draw <= 6:
    return "前置機動"
  if draw <= 10:
    return "居中順走"
  return "後上"


def detect_meeting_params(page) -> tuple[str, str]:
  info = page.evaluate("""
    () => ({
      url: window.location.href,
      text: document.body ? document.body.innerText.slice(0, 1500) : ""
    })
    """)
  url = info.get("url", "")
  text = info.get("text", "")

  race_date = os.getenv("HKJC_RACE_DATE", "").strip()
  course = os.getenv("HKJC_RACE_COURSE", "").strip().upper()

  if not race_date:
    m_url_date = re.search(r"raceDate=(\d{4}/\d{2}/\d{2})", url, re.I)
    if m_url_date:
      race_date = m_url_date.group(1)
    else:
      m_txt_date = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", text)
      if m_txt_date:
        y, m, d = m_txt_date.groups()
        race_date = f"{y}/{int(m):02d}/{int(d):02d}"
      else:
        m_slash = re.search(r"(\d{2})/(\d{2})/(\d{4})", text)
        if m_slash:
          d, m, y = m_slash.groups()
          race_date = f"{y}/{m}/{d}"

  if course not in {"ST", "HV"}:
    if "Racecourse=HV" in url or "跑馬地" in text[:400]:
      course = "HV"
    else:
      course = "ST"

  return race_date, course


def extract_race_meta(body_text: str, default_course: str) -> dict[str, str]:
  snippet = re.sub(r"\s+", " ", body_text or "")[:1200]

  meta: dict[str, str] = {
      "distance": "",
      "course": "跑馬地" if default_course == "HV" else "沙田",
      "class_name": "N/A",
      "track_feature": "標準賽道",
  }

  dist_match = re.search(r"(\d{3,4})\s*米", snippet)
  if dist_match:
    meta["distance"] = dist_match.group(1)

  if "跑馬地" in snippet[:350]:
    meta["course"] = "跑馬地"
  elif "沙田" in snippet[:350]:
    meta["course"] = "沙田"

  class_match = re.search(
      r"(第[一二三四五六]班|公開賽|表列賽|三級賽|二級賽|一級賽)",
      snippet,
  )
  if class_match:
    meta["class_name"] = class_match.group(1)

  track_match = re.search(
      r'["“]?(A\+3|B\+2|C\+3|A|B|C)["”]?\s*賽道',
      snippet,
  )
  if track_match:
    meta["track_feature"] = f'"{track_match.group(1)}" 賽道'
  elif "全天候" in snippet or "泥地" in snippet:
    meta["track_feature"] = "全天候跑道"
  elif "草地" in snippet:
    meta["track_feature"] = "草地跑道"

  return meta


def parse_runners_from_dom(
    page, default_course: str
) -> tuple[list[dict[str, Any]], dict[str, str], dict[str, Any]]:
  eval_result = page.evaluate("""
    () => {
      const title = document.title || "";
      const bodyText = document.body ? document.body.innerText : "";
      const tables = Array.from(document.querySelectorAll("table"));
      const extractedTables = tables.map((tbl, idx) => {
        const rows = Array.from(tbl.querySelectorAll("tr")).map(tr =>
          Array.from(tr.querySelectorAll("th, td")).map(cell =>
            cell.innerText.replace(/\\s+/g, " ").trim()
          )
        );
        return { index: idx, rowCount: rows.length, rows };
      });
      return {
        title,
        url: window.location.href,
        bodySnippet: bodyText.slice(0, 1400),
        tables: extractedTables
      };
    }
    """)

  runners: list[dict[str, Any]] = []
  meta = extract_race_meta(eval_result.get("bodySnippet", ""), default_course)

  for tbl in eval_result.get("tables", []):
    rows = tbl.get("rows", [])
    if len(rows) < 5:
      continue

    header_idx = -1
    col_map: dict[str, int] = {}

    for r_idx, row in enumerate(rows[:4]):
      joined = " ".join(row)
      if ("馬名" in joined) and ("檔位" in joined) and ("負磅" in joined):
        header_idx = r_idx
        for c_idx, col_name in enumerate(row):
          clean_col = col_name.replace(" ", "")
          if clean_col in {"馬匹編號", "編號", "馬號"}:
            col_map["no"] = c_idx
          elif clean_col == "馬名":
            col_map["name"] = c_idx
          elif clean_col in {"負磅", "實際負磅"}:
            col_map["weight"] = c_idx
          elif clean_col == "檔位":
            col_map["draw"] = c_idx
          elif clean_col == "評分":
            col_map["rating"] = c_idx
          elif "排位體重" in clean_col or clean_col == "體重":
            col_map["body_weight"] = c_idx
          elif clean_col == "騎師":
            col_map["jockey"] = c_idx
          elif clean_col == "練馬師":
            col_map["trainer"] = c_idx
          elif "近績" in clean_col or "往績" in clean_col:
            col_map["recent_runs"] = c_idx
        break

    if header_idx == -1 or "no" not in col_map or "name" not in col_map:
      continue

    seen_nos: set[int] = set()
    for row in rows[header_idx + 1 :]:
      if len(row) <= max(col_map.values()):
        continue

      raw_no = row[col_map["no"]].strip()
      if not re.fullmatch(r"\d{1,2}", raw_no):
        continue
      horse_no = int(raw_no)
      if not (1 <= horse_no <= 16) or horse_no in seen_nos:
        continue

      horse_name = row[col_map["name"]].strip()
      if not horse_name or re.fullmatch(r"[A-Za-z]\d{3}", horse_name):
        continue

      weight_lbs = (
          safe_float(row[col_map["weight"]], 125.0)
          if "weight" in col_map
          else 125.0
      )
      if weight_lbs is None or not (100.0 <= weight_lbs <= 140.0):
        weight_lbs = 125.0

      draw = safe_int(row[col_map["draw"]], 7) if "draw" in col_map else 7
      if not (1 <= draw <= 16):
        draw = 7

      rating = (
          safe_float(row[col_map["rating"]], 60.0)
          if "rating" in col_map
          else 60.0
      )
      if rating is None or not (10.0 <= rating <= 150.0):
        rating = 60.0

      # 解析排位體重（HKJC 馬匹體重合理範圍 850 - 1400 磅）
      body_weight: int | str = "N/A"
      if "body_weight" in col_map:
        bw_val = safe_float(row[col_map["body_weight"]], None)
        if bw_val is not None and 850 <= bw_val <= 1400:
          body_weight = int(round(bw_val))

      jockey = (
          clean_person_name(row[col_map["jockey"]], "N/A")
          if "jockey" in col_map
          else "N/A"
      )
      trainer = (
          clean_person_name(row[col_map["trainer"]], "N/A")
          if "trainer" in col_map
          else "N/A"
      )
      recent_runs = (
          row[col_map["recent_runs"]].strip() or "N/A"
          if "recent_runs" in col_map
          else "N/A"
      )
      running_style = infer_running_style(
          recent_runs, draw, meta.get("distance", "1400")
      )

      seen_nos.add(horse_no)
      runners.append({
          "no": horse_no,
          "name": horse_name,
          "draw": draw,
          "weight_lbs": weight_lbs,
          "rating": rating,
          "jockey": jockey,
          "trainer": trainer,
          "recent_runs": recent_runs,
          "running_style": running_style,
          "body_weight": body_weight,
          "win_odds": None,
      })

    if len(runners) >= 4:
      break

  return runners, meta, eval_result


def extract_odds_from_json_tree(node: Any, odds_map: dict[int, float]) -> None:
  """僅由明確嘅 GraphQL 賠率欄位提取獨贏賠率（避免誤抓場次編號）。"""
  if isinstance(node, dict):
    no_val = None
    for k in ("runnerNo", "horseNo", "clothNo"):
      if k in node and node[k] is not None:
        cand = safe_int(node[k], -1)
        if 1 <= cand <= 16:
          no_val = cand
          break

    if no_val is not None:
      for ok in ("winOdds", "currentOdds"):
        if ok in node and node[ok] not in (None, "", "-", "---"):
          raw_o = node[ok]
          if isinstance(raw_o, dict):
            for sub_k in ("oddsValue", "value", "winOdds"):
              if sub_k in raw_o:
                o_float = safe_float(raw_o[sub_k], None)
                if o_float is not None and 1.1 <= o_float <= 999.0:
                  odds_map[no_val] = o_float
                  break
          else:
            o_float = safe_float(raw_o, None)
            if o_float is not None and 1.1 <= o_float <= 999.0:
              odds_map[no_val] = o_float
              break

    for v in node.values():
      extract_odds_from_json_tree(v, odds_map)
  elif isinstance(node, list):
    for item in node:
      extract_odds_from_json_tree(item, odds_map)


def fetch_win_odds_for_race(
    page, race_date: str, course: str, race_no: int, runner_names: dict[int, str]
) -> tuple[dict[int, float], dict[str, Any]]:
  """抓取 bet.hkjc.com 實時獨贏賠率（嚴格要求帶小數點賠率 >= 1.1，防止未開盤誤抓場次號碼）。"""
  date_hyphen = race_date.replace("/", "-")
  odds_url = f"{BET_WP_BASE_URL}/{date_hyphen}/{course}/{race_no}"
  odds_map: dict[int, float] = {}
  graphql_hits: list[dict[str, Any]] = []

  def on_resp(resp: Response) -> None:
    if "graphql" not in resp.url.lower():
      return
    try:
      payload = resp.json()
      if isinstance(payload, dict):
        graphql_hits.append(payload)
        extract_odds_from_json_tree(payload, odds_map)
    except Exception:
      pass

  page.on("response", on_resp)
  try:
    page.goto(odds_url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(4500)

    # 僅在頁面真係有小數點賠率（例如 2.4、15.0）時先由 DOM 補充提取
    if len(odds_map) < len(runner_names):
      dom_rows = page.evaluate("""
        () => {
          const rows = Array.from(document.querySelectorAll("tr, [role='row']"));
          return rows.map(r => r.innerText.replace(/\\s+/g, " ").trim()).filter(Boolean);
        }
        """)
      for row_txt in dom_rows:
        for h_no, h_name in runner_names.items():
          if h_no in odds_map or not h_name or h_name not in row_txt:
            continue
          # 必須匹配帶小數點嘅賠率（如 3.2、18.5），過濾純整數場次號
          after_name = row_txt.split(h_name, 1)[1]
          m = re.search(r"\b(\d{1,3}\.\d{1,2})\b", after_name)
          if m:
            val = safe_float(m.group(1), None)
            if val is not None and 1.1 <= val <= 500.0:
              odds_map[h_no] = val
  except Exception as exc:
    print(f"Race {race_no} odds fetch warning: {exc}")
  finally:
    page.remove_listener("response", on_resp)

  # 若整場只有 1-2 隻勉強匹配，多數屬於未開盤雜訊，直接清空保持 N/A
  if len(odds_map) < 3:
    odds_map.clear()

  diag = {
      "race_no": race_no,
      "odds_url": odds_url,
      "graphql_count": len(graphql_hits),
      "matched_odds_count": len(odds_map),
      "sample_odds": dict(list(odds_map.items())[:5]),
  }
  return odds_map, diag


def fetch_hkjc_race_data() -> dict[str, Any]:
  races: list[dict[str, Any]] = []
  debug_summary: list[dict[str, Any]] = []
  odds_diagnostics: list[dict[str, Any]] = []
  max_races_to_check = int(os.getenv("HKJC_MAX_RACES", "11"))

  with sync_playwright() as p:
    browser = p.chromium.launch(
        headless=True,
        args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
    )
    context = browser.new_context(
        locale="zh-HK",
        timezone_id="Asia/Hong_Kong",
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1440, "height": 900},
    )
    page = context.new_page()

    def route_handler(route):
      if route.request.resource_type in {"image", "media", "font"}:
        route.abort()
      else:
        route.continue_()

    page.route("**/*", route_handler)

    try:
      initial_url = build_target_url(
          race_date=os.getenv("HKJC_RACE_DATE", "").strip(),
          course=os.getenv("HKJC_RACE_COURSE", "").strip().upper(),
          race_no=1,
      )
      print(f"Loading initial racecard: {initial_url}")
      page.goto(initial_url, wait_until="domcontentloaded", timeout=60000)
      page.wait_for_timeout(3500)

      race_date, course = detect_meeting_params(page)
      print(f"Detected meeting -> race_date={race_date}, course={course}")

      for race_no in range(1, max_races_to_check + 1):
        target_url = build_target_url(
            race_date=race_date, course=course, race_no=race_no
        )
        if race_no > 1:
          print(f"Fetching Race {race_no} card: {target_url}")
          page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
          page.wait_for_timeout(2500)

        runners, meta, eval_info = parse_runners_from_dom(page, course)

        if race_no in {1, 2}:
          debug_summary.append({
              "race_no": race_no,
              "url": eval_info.get("url"),
              "meta": meta,
              "runner_count": len(runners),
          })

        if not runners:
          print(f"No runners found for Race {race_no}, stopping loop.")
          break

        if (
            races
            and runners[0]["name"] == races[0]["runners"][0]["name"]
            and len(runners) == len(races[0]["runners"])
        ):
          print(
              f"Race {race_no} returned identical runners to Race 1, stopping."
          )
          break

        races.append({
            "race_no": race_no,
            "distance": meta.get("distance", ""),
            "course": meta.get("course", ""),
            "class_name": meta.get("class_name", "N/A"),
            "track_feature": meta.get("track_feature", "標準賽道"),
            "runners": runners,
        })
        print(f"Race {race_no}: extracted {len(runners)} runners.")

      if race_date and races:
        print("Fetching live win odds from bet.hkjc.com...")
        for race_obj in races:
          r_no = race_obj["race_no"]
          runner_names = {r["no"]: r["name"] for r in race_obj["runners"]}
          odds_map, o_diag = fetch_win_odds_for_race(
              page, race_date, course, r_no, runner_names
          )
          odds_diagnostics.append(o_diag)
          for r in race_obj["runners"]:
            if r["no"] in odds_map:
              r["win_odds"] = odds_map[r["no"]]
          print(
              f"Race {r_no} odds: matched {len(odds_map)}/{len(runner_names)} runners."
          )
    finally:
      browser.close()

  RAW_DEBUG_PATH.parent.mkdir(parents=True, exist_ok=True)
  RAW_DEBUG_PATH.write_text(
      json.dumps(
          {
              "racecard_summary": debug_summary,
              "odds_diagnostics": odds_diagnostics,
          },
          ensure_ascii=False,
          indent=2,
      ),
      encoding="utf-8",
  )

  result = {
      "race_date": race_date or datetime.now(HKT).strftime("%Y-%m-%d"),
      "course_code": course,
      "source_url": build_target_url(
          race_date=race_date, course=course, race_no=1
      ),
      "fetched_at_hkt": datetime.now(HKT).isoformat(),
      "races": races,
  }

  OUTPUT_DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
  OUTPUT_DATA_PATH.write_text(
      json.dumps(result, ensure_ascii=False, indent=2),
      encoding="utf-8",
  )
  print(f"Saved {len(races)} races to {OUTPUT_DATA_PATH}")
  return result


if __name__ == "__main__":
  fetch_hkjc_race_data()
