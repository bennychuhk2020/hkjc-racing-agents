from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright, BrowserContext, Page, Response

RAW_DEBUG_PATH = Path("data/raw_graphql_debug.json")
OUTPUT_DATA_PATH = Path("data/upcoming_races.json")

HKT = timezone(timedelta(hours=8))

LOCAL_RACECARD_URL = (
    "https://racing.hkjc.com/zh-hk/local/information/racecard"
)
OVERSEAS_RACECARD_URL = (
    "https://racing.hkjc.com/zh-hk/overseas/information/racecard"
)
BET_WP_BASE_URL = "https://bet.hkjc.com/ch/racing/wp"

BLOCKED_RESOURCE_TYPES = {"image", "media", "font", "stylesheet"}
BLOCKED_URL_KEYWORDS = (
    "google-analytics",
    "googletagmanager",
    "doubleclick",
    "facebook",
    "hotjar",
    "clarity.ms",
)


def build_local_url(
    race_date: str = "", course: str = "", race_no: int | None = None
) -> str:
  base_url = os.getenv("HKJC_TARGET_URL", LOCAL_RACECARD_URL).strip()
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


def build_overseas_url(
    race_date: str = "", simulcast_code: str = "S1", race_no: int | None = None
) -> str:
  params = []
  if race_date:
    params.append(f"raceDate={race_date}")
  if simulcast_code:
    params.append(f"Simulcast={simulcast_code}")
  if race_no is not None:
    params.append(f"RaceNo={race_no}")
  if not params:
    return OVERSEAS_RACECARD_URL
  return f"{OVERSEAS_RACECARD_URL}?{'&'.join(params)}"


def parse_simulcast_spec(spec_str: str) -> dict[str, list[int]]:
  """解析如 'S1:1-3,S2:1-9,S3:1' 的指定海外場次清單。"""
  result: dict[str, list[int]] = {}
  if not spec_str.strip():
    return result
  for part in spec_str.split(","):
    part = part.strip().upper()
    if not part:
      continue
    m = re.fullmatch(r"(S\d+)(?::(\d+)(?:-(\d+))?)?", part)
    if not m:
      continue
    s_code, start_s, end_s = m.groups()
    if start_s is None:
      result[s_code] = list(range(1, 13))
    elif end_s is None:
      result[s_code] = [int(start_s)]
    else:
      result[s_code] = list(range(int(start_s), int(end_s) + 1))
  return result


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

  if draw <= 2 and dist <= 1200:
    return "領放"
  if draw <= 6:
    return "前置機動"
  if draw <= 10:
    return "居中順走"
  return "後上"


async def configure_fast_context(context: BrowserContext) -> None:
  async def route_handler(route):
    req = route.request
    if req.resource_type in BLOCKED_RESOURCE_TYPES:
      await route.abort()
      return
    url_l = req.url.lower()
    if any(k in url_l for k in BLOCKED_URL_KEYWORDS):
      await route.abort()
      return
    await route.continue_()

  await context.route("**/*", route_handler)


async def detect_meeting_params(page: Page) -> tuple[str, str]:
  info = await page.evaluate("""
    () => ({
      url: window.location.href,
      text: document.body ? document.body.innerText.slice(0, 2000) : ""
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


async def discover_simulcast_meetings(
    page: Page, race_date: str
) -> dict[str, list[int]]:
  """自動偵測 HKJC 海外賽事頁面中的所有轉播組別（例如 S1, S2, S3）及對應場次編號。"""
  url = build_overseas_url(race_date=race_date, simulcast_code="S1", race_no=1)
  discovered: dict[str, set[int]] = {}
  try:
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(1200)
    links_and_text = await page.evaluate("""
      () => {
        const hrefs = Array.from(document.querySelectorAll("a[href]")).map(a => a.href);
        const body = document.body ? document.body.innerText.slice(0, 4000) : "";
        return { hrefs, body };
      }
    """)
    for href in links_and_text.get("hrefs", []):
      m_s = re.search(r"Simulcast=(S\d+)", href, re.I)
      m_r = re.search(r"RaceNo=(\d+)", href, re.I)
      if m_s:
        s_code = m_s.group(1).upper()
        discovered.setdefault(s_code, set()).add(
            int(m_r.group(1)) if m_r else 1
        )

    # 同時掃描頁面文字中的 S1-1, S2-9 等場次標籤
    for m_tag in re.finditer(
        r"\b(S\d+)\s*[-－]\s*(\d{1,2})\b",
        links_and_text.get("body", ""),
        re.I,
    ):
      s_code = m_tag.group(1).upper()
      r_no = int(m_tag.group(2))
      if 1 <= r_no <= 15:
        discovered.setdefault(s_code, set()).add(r_no)
  except Exception as exc:
    print(f"Simulcast discovery note: {exc}")

  return {k: sorted(v) for k, v in sorted(discovered.items())}


def extract_race_meta(
    body_text: str,
    default_course: str,
    race_no: int = 1,
    simulcast_code: str = "",
) -> dict[str, str]:
  full_text = re.sub(r"\s+", " ", body_text or "")

  if simulcast_code:
    anchor = re.search(
        rf"({re.escape(simulcast_code)}\s*[-－]\s*{race_no}|第\s*{race_no}\s*場)",
        full_text,
        re.I,
    )
  else:
    anchor = re.search(rf"第\s*{race_no}\s*場", full_text)

  snippet = (
      full_text[anchor.start() : anchor.start() + 1000]
      if anchor
      else full_text[:2800]
  )

  meta: dict[str, str] = {
      "distance": "",
      "course": (
          f"海外轉播 ({simulcast_code})"
          if simulcast_code
          else ("跑馬地" if default_course == "HV" else "沙田")
      ),
      "class_name": "海外賽事" if simulcast_code else "N/A",
      "track_feature": "草地跑道" if simulcast_code else "標準賽道",
  }

  dist_match = re.search(r"(\d{3,4})\s*米", snippet)
  if dist_match:
    meta["distance"] = dist_match.group(1)

  if simulcast_code:
    # 嘗試提取海外馬場名稱（例如 雅士谷、東京、阪神、沙丘園、美丹 等）
    venue_match = re.search(
        r"(?:馬場|場地|轉播)\s*[:：]?\s*([^\s｜|（(]{2,10})", snippet
    )
    if venue_match and "賽道" not in venue_match.group(1):
      meta["course"] = f"{venue_match.group(1)} ({simulcast_code})"
  else:
    if "跑馬地" in snippet[:300]:
      meta["course"] = "跑馬地"
    elif "沙田" in snippet[:300]:
      meta["course"] = "沙田"

  class_match = re.search(
      r"(第[一二三四五六]班|公開賽|表列賽|三級賽|二級賽|一級賽|國際[一二三]級賽|讓賽)",
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
  elif "全天候" in snippet or "泥地" in snippet or "膠沙地" in snippet:
    meta["track_feature"] = "全天候/泥地"
  elif "草地" in snippet:
    meta["track_feature"] = "草地跑道"

  return meta


async def parse_runners_from_dom(
    page: Page,
    default_course: str,
    race_no: int = 1,
    simulcast_code: str = "",
) -> tuple[list[dict[str, Any]], dict[str, str], dict[str, Any]]:
  eval_result = await page.evaluate("""
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
        bodySnippet: bodyText.slice(0, 5000),
        tables: extractedTables
      };
    }
    """)

  runners: list[dict[str, Any]] = []
  meta = extract_race_meta(
      eval_result.get("bodySnippet", ""),
      default_course,
      race_no=race_no,
      simulcast_code=simulcast_code,
  )

  for tbl in eval_result.get("tables", []):
    rows = tbl.get("rows", [])
    if len(rows) < 4:
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
          elif "負磅" in clean_col and ("加減" in clean_col or "+/-" in clean_col):
            col_map["weight_diff"] = c_idx
          elif clean_col == "檔位":
            col_map["draw"] = c_idx
          elif clean_col == "評分":
            col_map["rating"] = c_idx
          elif "評分" in clean_col and ("加減" in clean_col or "+/-" in clean_col):
            col_map["rating_diff"] = c_idx
          elif "排位體重" in clean_col or clean_col == "體重":
            col_map["body_weight"] = c_idx
          elif "體重" in clean_col and ("加減" in clean_col or "+/-" in clean_col):
            col_map["bw_diff"] = c_idx
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
      # 海外賽事有時多達 24 匹出賽馬
      if not (1 <= horse_no <= 24) or horse_no in seen_nos:
        continue

      horse_name = row[col_map["name"]].strip()
      if not horse_name or re.fullmatch(r"[A-Za-z]\d{3}", horse_name):
        continue

      weight_lbs = (
          safe_float(row[col_map["weight"]], 125.0)
          if "weight" in col_map
          else 125.0
      )
      if weight_lbs is None or not (100.0 <= weight_lbs <= 145.0):
        weight_lbs = 125.0

      weight_diff = (
          row[col_map["weight_diff"]].strip()
          if "weight_diff" in col_map
          else ""
      )
      if weight_diff and weight_diff not in {"-", "--", "0"}:
        if not weight_diff.startswith(("+", "-")) and weight_diff.isdigit():
          weight_diff = f"+{weight_diff}"
        weight_display = f"{int(weight_lbs)} ({weight_diff})"
      else:
        weight_display = f"{int(weight_lbs)}"

      draw = safe_int(row[col_map["draw"]], 7) if "draw" in col_map else 7
      if not (1 <= draw <= 24):
        draw = 7

      rating = (
          safe_float(row[col_map["rating"]], 80.0 if simulcast_code else 60.0)
          if "rating" in col_map
          else (80.0 if simulcast_code else 60.0)
      )
      if rating is None or not (10.0 <= rating <= 150.0):
        rating = 80.0 if simulcast_code else 60.0

      rating_diff = (
          row[col_map["rating_diff"]].strip()
          if "rating_diff" in col_map
          else ""
      )
      if rating_diff and rating_diff not in {"-", "--", "0"}:
        if not rating_diff.startswith(("+", "-")) and rating_diff.isdigit():
          rating_diff = f"+{rating_diff}"
        rating_display = f"{int(rating)} ({rating_diff})"
      else:
        rating_display = f"{int(rating)}"

      body_weight: int | str = "N/A"
      bw_display = "N/A"
      if "body_weight" in col_map:
        raw_bw_cell = row[col_map["body_weight"]].strip()
        bw_val = safe_float(raw_bw_cell, None)
        if bw_val is not None and 850 <= bw_val <= 1400:
          body_weight = int(round(bw_val))
          bw_diff = (
              row[col_map["bw_diff"]].strip()
              if "bw_diff" in col_map
              else ""
          )
          if bw_diff and bw_diff not in {"-", "--"}:
            if not bw_diff.startswith(("+", "-")) and bw_diff.isdigit():
              bw_diff = f"+{bw_diff}"
            bw_display = f"{body_weight} ({bw_diff})"
          else:
            bw_display = (
                raw_bw_cell if "(" in raw_bw_cell else str(body_weight)
            )

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
          "weight_display": weight_display,
          "rating": rating,
          "rating_display": rating_display,
          "jockey": jockey,
          "trainer": trainer,
          "recent_runs": recent_runs,
          "running_style": running_style,
          "body_weight": body_weight,
          "bw_display": bw_display,
          "win_odds": None,
      })

    if len(runners) >= 4:
      break

  return runners, meta, eval_result


def extract_odds_from_json_tree(node: Any, odds_map: dict[int, float]) -> None:
  if isinstance(node, dict):
    no_val = None
    for k in ("runnerNo", "horseNo", "clothNo"):
      if k in node and node[k] is not None:
        cand = safe_int(node[k], -1)
        if 1 <= cand <= 24:
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


async def _parse_graphql_resp(
    resp: Response,
    graphql_hits: list[dict[str, Any]],
    odds_map: dict[int, float],
) -> None:
  try:
    payload = await resp.json()
    if isinstance(payload, dict):
      graphql_hits.append(payload)
      extract_odds_from_json_tree(payload, odds_map)
  except Exception:
    pass


async def fetch_win_odds_worker(
    sem: asyncio.Semaphore,
    context: BrowserContext,
    race_date: str,
    venue_code: str,
    race_no: int,
    race_label: str,
    runner_names: dict[int, str],
) -> tuple[str, dict[int, float], dict[str, Any]]:
  """支援本地 (ST/HV) 及海外轉播 (S1/S2/S3) 的並發獨贏賠率抓取。"""
  date_hyphen = race_date.replace("/", "-")
  odds_url = f"{BET_WP_BASE_URL}/{date_hyphen}/{venue_code}/{race_no}"
  odds_map: dict[int, float] = {}
  graphql_hits: list[dict[str, Any]] = []

  async with sem:
    page = await context.new_page()

    def on_resp(resp: Response) -> None:
      if "graphql" not in resp.url.lower():
        return
      asyncio.create_task(_parse_graphql_resp(resp, graphql_hits, odds_map))

    page.on("response", on_resp)
    try:
      await page.goto(odds_url, wait_until="domcontentloaded", timeout=20000)
      await page.wait_for_timeout(1600)

      if len(odds_map) < len(runner_names):
        dom_rows = await page.evaluate("""
          () => {
            const rows = Array.from(document.querySelectorAll("tr, [role='row']"));
            return rows.map(r => r.innerText.replace(/\\s+/g, " ").trim()).filter(Boolean);
          }
          """)
        for row_txt in dom_rows:
          for h_no, h_name in runner_names.items():
            if h_no in odds_map or not h_name or h_name not in row_txt:
              continue
            after_name = row_txt.split(h_name, 1)[1]
            m = re.search(r"\b(\d{1,3}\.\d{1,2})\b", after_name)
            if m:
              val = safe_float(m.group(1), None)
              if val is not None and 1.1 <= val <= 500.0:
                odds_map[h_no] = val
    except Exception as exc:
      print(f"{race_label} odds fetch warning: {exc}")
    finally:
      page.remove_listener("response", on_resp)
      await page.close()

  if len(odds_map) < 3:
    odds_map.clear()

  diag = {
      "race_label": race_label,
      "odds_url": odds_url,
      "graphql_count": len(graphql_hits),
      "matched_odds_count": len(odds_map),
      "sample_odds": dict(list(odds_map.items())[:5]),
  }
  return race_label, odds_map, diag


async def fetch_hkjc_race_data_async() -> dict[str, Any]:
  races: list[dict[str, Any]] = []
  debug_summary: list[dict[str, Any]] = []
  odds_diagnostics: list[dict[str, Any]] = []

  max_local_races = int(os.getenv("HKJC_MAX_RACES", "11"))
  include_overseas = (
      os.getenv("HKJC_INCLUDE_OVERSEAS", "true").lower() == "true"
  )
  simulcast_spec = parse_simulcast_spec(
      os.getenv("HKJC_SIMULCAST_SPEC", "")
  )
  concurrency = int(os.getenv("HKJC_CONCURRENCY", "6"))

  async with async_playwright() as p:
    browser = await p.chromium.launch(
        headless=True,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
        ],
    )
    context = await browser.new_context(
        locale="zh-HK",
        timezone_id="Asia/Hong_Kong",
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1280, "height": 800},
    )
    await configure_fast_context(context)
    page = await context.new_page()

    try:
      # 1. 抓取本地賽馬排位表
      initial_url = build_local_url(
          race_date=os.getenv("HKJC_RACE_DATE", "").strip(),
          course=os.getenv("HKJC_RACE_COURSE", "").strip().upper(),
          race_no=1,
      )
      print(f"Loading initial local racecard: {initial_url}")
      await page.goto(initial_url, wait_until="domcontentloaded", timeout=40000)
      await page.wait_for_selector("table", timeout=12000)
      await page.wait_for_timeout(500)

      race_date, course = await detect_meeting_params(page)
      print(f"Detected meeting -> race_date={race_date}, local_course={course}")

      local_first_runner = ""
      for race_no in range(1, max_local_races + 1):
        target_url = build_local_url(
            race_date=race_date, course=course, race_no=race_no
        )
        if race_no > 1:
          await page.goto(
              target_url, wait_until="domcontentloaded", timeout=35000
          )
          await page.wait_for_selector("table", timeout=10000)
          await page.wait_for_timeout(350)

        runners, meta, eval_info = await parse_runners_from_dom(
            page, course, race_no=race_no
        )
        if not runners:
          break
        if race_no == 1:
          local_first_runner = runners[0]["name"]
        elif runners[0]["name"] == local_first_runner:
          break

        race_label = f"第{race_no}場"
        races.append({
            "race_type": "local",
            "venue_code": course,
            "race_no": race_no,
            "race_label": race_label,
            "distance": meta.get("distance", ""),
            "course": meta.get("course", ""),
            "class_name": meta.get("class_name", "N/A"),
            "track_feature": meta.get("track_feature", "標準賽道"),
            "runners": runners,
        })
        print(f"[Local] {race_label}: extracted {len(runners)} runners.")

      # 2. 抓取海外轉播賽馬排位表 (S1-1..S1-3, S2-1..S2-9, S3-1...)
      if include_overseas and race_date:
        target_simulcasts = (
            simulcast_spec
            if simulcast_spec
            else await discover_simulcast_meetings(page, race_date)
        )
        print(f"Target overseas simulcasts: {target_simulcasts}")

        for s_code, race_nos in target_simulcasts.items():
          first_overseas_runner = ""
          for r_no in race_nos:
            s_url = build_overseas_url(
                race_date=race_date, simulcast_code=s_code, race_no=r_no
            )
            try:
              await page.goto(
                  s_url, wait_until="domcontentloaded", timeout=30000
              )
              await page.wait_for_selector("table", timeout=8000)
              await page.wait_for_timeout(350)
            except Exception:
              print(f"[Overseas] {s_code}-{r_no} page unavailable, skipping.")
              break

            runners, meta, _ = await parse_runners_from_dom(
                page, s_code, race_no=r_no, simulcast_code=s_code
            )
            if not runners:
              break
            if not first_overseas_runner:
              first_overseas_runner = runners[0]["name"]
            elif r_no > race_nos[0] and runners[0]["name"] == first_overseas_runner:
              break

            race_label = f"{s_code}-{r_no}"
            races.append({
                "race_type": "overseas",
                "venue_code": s_code,
                "race_no": r_no,
                "race_label": race_label,
                "distance": meta.get("distance", ""),
                "course": meta.get("course", f"海外轉播 ({s_code})"),
                "class_name": meta.get("class_name", "海外賽事"),
                "track_feature": meta.get("track_feature", "草地跑道"),
                "runners": runners,
            })
            print(
                f"[Overseas] {race_label}: extracted {len(runners)} runners."
            )

      await page.close()

      # 3. 並發抓取所有本地 + 海外場次實時獨贏賠率
      if race_date and races:
        sem = asyncio.Semaphore(concurrency)
        print(
            f"Fetching live win odds concurrently for {len(races)} races (workers={concurrency})..."
        )
        odds_tasks = [
            fetch_win_odds_worker(
                sem,
                context,
                race_date,
                race_obj["venue_code"],
                race_obj["race_no"],
                race_obj["race_label"],
                {r["no"]: r["name"] for r in race_obj["runners"]},
            )
            for race_obj in races
        ]
        odds_results = await asyncio.gather(*odds_tasks)
        odds_by_label = {lbl: o_map for lbl, o_map, _ in odds_results}
        odds_diagnostics = [diag for _, _, diag in odds_results]

        for race_obj in races:
          lbl = race_obj["race_label"]
          o_map = odds_by_label.get(lbl, {})
          for r in race_obj["runners"]:
            if r["no"] in o_map:
              r["win_odds"] = o_map[r["no"]]
          print(
              f"{lbl} odds: matched {len(o_map)}/{len(race_obj['runners'])} runners."
          )
    finally:
      await browser.close()

  RAW_DEBUG_PATH.parent.mkdir(parents=True, exist_ok=True)
  RAW_DEBUG_PATH.write_text(
      json.dumps(
          {
              "total_races": len(races),
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
      "source_url": build_local_url(
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
  print(f"Saved {len(races)} total races (local + overseas) to {OUTPUT_DATA_PATH}")
  return result


def fetch_hkjc_race_data() -> dict[str, Any]:
  return asyncio.run(fetch_hkjc_race_data_async())


if __name__ == "__main__":
  fetch_hkjc_race_data()
