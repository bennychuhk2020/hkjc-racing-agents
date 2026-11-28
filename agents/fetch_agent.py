from __future__ import annotations

import asyncio
import json
import os
import re
import time
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
OVERSEAS_ASPX_URL = "https://racing.hkjc.com/racing/overseas/chinese/racecard.aspx"
BET_WP_BASE_URL = "https://bet.hkjc.com/ch/racing/wp"

BLOCKED_RESOURCE_TYPES = {"image", "media", "font"}
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


def build_overseas_aspx_url(
    race_date: str, simulcast_code: str, race_no: int
) -> str:
  compact_date = re.sub(r"[^\d]", "", race_date or "")
  return f"{OVERSEAS_ASPX_URL}?para=/{compact_date}/{simulcast_code.upper()}/{race_no}"


def parse_simulcast_spec(spec_str: str) -> dict[str, list[int]]:
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


def parse_combined_horse_jt(text: str) -> tuple[str, str, str]:
  """從合併字串（例如 '登天橋騎薛恩練白敬雅'）拆出馬名、騎師、練馬師。"""
  cleaned = re.sub(r"\s+", " ", text or "").strip()
  m = re.match(r"^(.+?)\s*騎\s*(.+?)\s*練\s*(.+)$", cleaned)
  if m:
    horse = re.sub(r"\s*\([a-zA-Z]+\)\s*$", "", m.group(1)).strip()
    jockey = clean_person_name(m.group(2), "N/A")
    trainer = clean_person_name(m.group(3), "N/A")
    return horse, jockey, trainer

  horse_only = re.sub(r"\s*\([a-zA-Z]+\)\s*$", "", cleaned).strip()
  return horse_only, "N/A", "N/A"


def estimate_overseas_rating(
    horse_no: int, weight_lbs: float, draw: int
) -> float:
  """當海外賽卡未提供官方評分時，按負磅、馬號與檔位推算具區分度的參考評分。"""
  weight_component = (weight_lbs - 120.0) * 1.15
  cloth_component = max(0.0, (15 - horse_no) * 0.45)
  draw_component = max(0.0, (9 - draw) * 0.25)
  est = 78.0 + weight_component + cloth_component + draw_component
  return round(max(55.0, min(115.0, est)), 1)


def infer_running_style(
    recent_runs: str, draw: int | None, distance_str: str
) -> str:
  runs = [int(x) for x in re.findall(r"\b\d+\b", recent_runs.replace("/", " "))]
  dist = safe_int(distance_str, 1400)
  d_val = draw if isinstance(draw, int) and 1 <= draw <= 24 else 7

  if runs:
    recent_3 = runs[:3]
    avg_pos = sum(recent_3) / len(recent_3)
    best_pos = min(recent_3)
    if best_pos == 1 and d_val <= 4 and dist <= 1400:
      return "領放"
    if avg_pos <= 4.0:
      return "前置機動"
    if avg_pos <= 7.5:
      return "居中順走"
    return "後上"

  if d_val <= 2 and dist <= 1200:
    return "領放"
  if d_val <= 6:
    return "前置機動"
  if d_val <= 10:
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


def extract_overseas_meta_from_text(
    full_text: str, s_code: str, race_no: int
) -> dict[str, str]:
  flat = re.sub(r"\s+", " ", full_text or "")
  meta = {
      "distance": "",
      "course": f"海外轉播 ({s_code})",
      "class_name": "海外賽事",
      "track_feature": "草地跑道",
  }

  dist_m = re.search(r"(\d{3,4})\s*米", flat)
  if dist_m:
    meta["distance"] = dist_m.group(1)

  venue_m = re.search(
      r"(沙丘園|雅士谷|費明頓|蘭域|玫瑰崗|考菲爾德|滿利谷|新市場|約克|古活|唐加士打|京士頓公園|東京|中山|阪神|京都|中京|美丹|邱吉爾園|聖雅尼塔|德爾馬|雅息特)\s*(?:馬場)?",
      flat,
  )
  if venue_m:
    v_name = venue_m.group(1)
    if not v_name.endswith("馬場"):
      v_name = f"{v_name}馬場"
    meta["course"] = f"{v_name} ({s_code})"

  class_match = re.search(
      r"(第[一二三四五六]班|公開賽|表列賽|三級賽|二級賽|一級賽|國際[一二三]級賽|讓賽)",
      flat,
  )
  if class_match:
    meta["class_name"] = class_match.group(1)

  if "全天候" in flat or "膠沙地" in flat or "泥地" in flat:
    meta["track_feature"] = "全天候/泥地"
  elif "草地" in flat:
    meta["track_feature"] = "草地跑道"

  return meta


def extract_race_meta(
    body_text: str,
    default_course: str,
    race_no: int = 1,
    simulcast_code: str = "",
) -> dict[str, str]:
  full_text = re.sub(r"\s+", " ", body_text or "")

  if simulcast_code:
    return extract_overseas_meta_from_text(full_text, simulcast_code, race_no)

  anchor = re.search(rf"第\s*{race_no}\s*場", full_text)
  snippet = (
      full_text[anchor.start() : anchor.start() + 1000]
      if anchor
      else full_text[:3500]
  )

  meta: dict[str, str] = {
      "distance": "",
      "course": "跑馬地" if default_course == "HV" else "沙田",
      "class_name": "N/A",
      "track_feature": "標準賽道",
  }

  dist_match = re.search(r"(\d{3,4})\s*米", snippet)
  if dist_match:
    meta["distance"] = dist_match.group(1)

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
      if not (1 <= horse_no <= 24) or horse_no in seen_nos:
        continue

      raw_name = row[col_map["name"]].strip()
      if (
          not raw_name
          or "(備)" in raw_name
          or re.fullmatch(r"[A-Za-z]\d{3}", raw_name)
      ):
        continue

      horse_name, parsed_jockey, parsed_trainer = parse_combined_horse_jt(
          raw_name
      )
      if not horse_name or "(備)" in horse_name:
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

      fallback_rating = (
          estimate_overseas_rating(horse_no, weight_lbs, draw)
          if simulcast_code
          else 60.0
      )
      rating = (
          safe_float(row[col_map["rating"]], fallback_rating)
          if "rating" in col_map
          else fallback_rating
      )
      if rating is None or not (10.0 <= rating <= 150.0):
        rating = fallback_rating

      rating_display = (
          f"{rating:.1f}" if simulcast_code and "rating" not in col_map else f"{int(round(rating))}"
      )

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
          clean_person_name(row[col_map["jockey"]], parsed_jockey)
          if "jockey" in col_map
          else parsed_jockey
      )
      trainer = (
          clean_person_name(row[col_map["trainer"]], parsed_trainer)
          if "trainer" in col_map
          else parsed_trainer
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


def _extract_odds_number(raw_o: Any) -> float | None:
  if isinstance(raw_o, dict):
    for sub_k in ("oddsValue", "value", "winOdds", "wpOdds", "odds"):
      if sub_k in raw_o:
        val = safe_float(raw_o[sub_k], None)
        if val is not None and 1.1 <= val <= 999.0:
          return val
    return None
  val = safe_float(raw_o, None)
  if val is not None and 1.1 <= val <= 999.0:
    return val
  return None


def extract_runners_from_bet_graphql(
    node: Any,
    collected: dict[int, dict[str, Any]],
    race_meta: dict[str, str] | None = None,
) -> None:
  if isinstance(node, dict):
    if race_meta is not None:
      dist_v = node.get("distance")
      if isinstance(dist_v, (int, float)) and 800 <= int(dist_v) <= 4000:
        race_meta["distance"] = str(int(dist_v))
      cls_v = node.get("raceClassCh") or node.get("classNameCh")
      if isinstance(cls_v, str) and cls_v.strip():
        race_meta["class_name"] = cls_v.strip()

    no_val = None
    for k in ("runnerNo", "horseNo", "clothNo", "no"):
      if k in node and node[k] is not None:
        cand = safe_int(node[k], -1)
        if 1 <= cand <= 24:
          no_val = cand
          break

    name_val = ""
    for nk in ("horseNameCh", "horseName", "nameCh", "name"):
      if isinstance(node.get(nk), str) and node[nk].strip():
        name_val = node[nk].strip()
        break

    if (
        no_val is not None
        and name_val
        and "(備)" not in name_val
        and not re.fullmatch(r"\d+", name_val)
    ):
      parsed_h, parsed_j, parsed_t = parse_combined_horse_jt(name_val)
      entry = collected.setdefault(
          no_val,
          {
              "no": no_val,
              "name": parsed_h or name_val,
              "draw": 7,
              "weight_lbs": 125.0,
              "weight_display": "125",
              "rating": None,
              "rating_display": "N/A",
              "jockey": parsed_j,
              "trainer": parsed_t,
              "recent_runs": "N/A",
              "running_style": "前置機動",
              "body_weight": "N/A",
              "bw_display": "N/A",
              "win_odds": None,
          },
      )
      if parsed_h:
        entry["name"] = parsed_h

      for dk in ("barrierDrawNumber", "draw", "barrier"):
        if dk in node and node[dk] is not None:
          d_int = safe_int(node[dk], -1)
          if 1 <= d_int <= 24:
            entry["draw"] = d_int
            break

      for wk in ("handicapWeight", "weight", "carriedWeight"):
        if wk in node and node[wk] is not None:
          w_flt = safe_float(node[wk], None)
          if w_flt is not None and 100.0 <= w_flt <= 145.0:
            entry["weight_lbs"] = w_flt
            entry["weight_display"] = f"{int(round(w_flt))}"
            break

      for rk in ("rating", "currentRating", "handicapRating"):
        if rk in node and node[rk] is not None:
          r_flt = safe_float(node[rk], None)
          if r_flt is not None and 10.0 <= r_flt <= 150.0:
            entry["rating"] = r_flt
            entry["rating_display"] = f"{int(round(r_flt))}"
            break

      j_raw = (
          node.get("jockey") or node.get("jockeyNameCh") or node.get("jockeyName")
      )
      if isinstance(j_raw, dict):
        j_raw = j_raw.get("nameCh") or j_raw.get("name") or ""
      if isinstance(j_raw, str) and j_raw.strip():
        entry["jockey"] = clean_person_name(j_raw, entry["jockey"])

      t_raw = (
          node.get("trainer")
          or node.get("trainerNameCh")
          or node.get("trainerName")
      )
      if isinstance(t_raw, dict):
        t_raw = t_raw.get("nameCh") or t_raw.get("name") or ""
      if isinstance(t_raw, str) and t_raw.strip():
        entry["trainer"] = clean_person_name(t_raw, entry["trainer"])

      for ok in ("winOdds", "currentOdds", "wpOdds", "odds"):
        if ok in node and node[ok] not in (None, "", "-", "---"):
          o_val = _extract_odds_number(node[ok])
          if o_val is not None:
            entry["win_odds"] = o_val
            break

    for v in node.values():
      extract_runners_from_bet_graphql(v, collected, race_meta)
  elif isinstance(node, list):
    for item in node:
      extract_runners_from_bet_graphql(item, collected, race_meta)


async def fetch_overseas_race_via_bet_page(
    context: BrowserContext,
    race_date: str,
    s_code: str,
    r_no: int,
) -> tuple[dict[str, Any] | None, str]:
  date_hyphen = race_date.replace("/", "-")
  s_upper = s_code.upper()
  bet_url = f"{BET_WP_BASE_URL}/{date_hyphen}/{s_upper}/{r_no}"
  collected: dict[int, dict[str, Any]] = {}
  gql_meta: dict[str, str] = {}
  page = await context.new_page()

  async def _handle_resp(resp: Response) -> None:
    if "graphql" not in resp.url.lower():
      return
    try:
      payload = await resp.json()
      if isinstance(payload, dict):
        extract_runners_from_bet_graphql(payload, collected, gql_meta)
    except Exception:
      pass

  def on_resp(resp: Response) -> None:
    asyncio.create_task(_handle_resp(resp))

  page.on("response", on_resp)
  full_text = ""
  try:
    await page.goto(bet_url, wait_until="domcontentloaded", timeout=15000)
    await page.wait_for_timeout(1300)

    final_url = page.url.upper()
    if f"/{s_upper}/{r_no}" not in final_url or date_hyphen not in final_url:
      return None, f"redirected:{page.url}"

    full_text = await page.evaluate(
        "() => document.body ? document.body.innerText : ''"
    )

    rows_data = await page.evaluate("""
      () => Array.from(document.querySelectorAll("tr, [role='row']")).map(r => {
        const cells = Array.from(r.querySelectorAll("th, td, [role='cell']"));
        return {
          cells: cells.map(c => c.innerText.replace(/\\s+/g, " ").trim()).filter(Boolean),
          text: r.innerText.replace(/\\s+/g, " ").trim()
        };
      })
    """)

    for item in rows_data:
      cells = item.get("cells", [])
      line = item.get("text", "")
      if "(備)" in line:
        continue

      h_no = None
      if cells and re.fullmatch(r"\d{1,2}", cells[0]):
        h_no = int(cells[0])
      else:
        m_no = re.match(r"^(\d{1,2})\s+", line)
        if m_no:
          h_no = int(m_no.group(1))

      if h_no is None or not (1 <= h_no <= 24):
        continue

      combined_str = ""
      for c in cells[1:]:
        if "騎" in c and "練" in c:
          combined_str = c
          break
      if not combined_str and len(cells) >= 2:
        combined_str = cells[1]
      if not combined_str:
        combined_str = re.sub(r"^\d{1,2}\s+", "", line)

      h_name, h_jockey, h_trainer = parse_combined_horse_jt(combined_str)
      if not h_name or "(備)" in h_name or re.fullmatch(r"[\d.]+", h_name):
        continue

      draw_val = 7
      weight_val = 125.0
      for cell in cells[2:]:
        if re.fullmatch(r"\d{1,2}", cell):
          cand_d = int(cell)
          if 1 <= cand_d <= 20:
            draw_val = cand_d
        elif re.fullmatch(r"1[0-4]\d", cell):
          weight_val = float(cell)

      if h_no not in collected:
        collected[h_no] = {
            "no": h_no,
            "name": h_name,
            "draw": draw_val,
            "weight_lbs": weight_val,
            "weight_display": f"{int(weight_val)}",
            "rating": None,
            "rating_display": "N/A",
            "jockey": h_jockey,
            "trainer": h_trainer,
            "recent_runs": "N/A",
            "running_style": "前置機動",
            "body_weight": "N/A",
            "bw_display": "N/A",
            "win_odds": None,
        }
      else:
        collected[h_no]["name"] = h_name
        if h_jockey != "N/A":
          collected[h_no]["jockey"] = h_jockey
        if h_trainer != "N/A":
          collected[h_no]["trainer"] = h_trainer
  except Exception:
    pass
  finally:
    page.remove_listener("response", on_resp)
    await page.close()

  if len(collected) < 4:
    return None, bet_url

  meta = extract_overseas_meta_from_text(full_text, s_upper, r_no)
  meta.update({k: v for k, v in gql_meta.items() if v})

  runners = []
  for k in sorted(collected.keys()):
    r = collected[k]
    if r.get("rating") is None:
      est_r = estimate_overseas_rating(r["no"], r["weight_lbs"], r["draw"])
      r["rating"] = est_r
      r["rating_display"] = f"{est_r:.1f}"
    r["running_style"] = infer_running_style(
        r["recent_runs"], r["draw"], meta.get("distance", "1600")
    )
    runners.append(r)

  return (
      {
          "race_type": "overseas",
          "venue_code": s_upper,
          "race_date_used": race_date,
          "race_no": r_no,
          "race_label": f"{s_upper}-{r_no}",
          "distance": meta.get("distance", ""),
          "course": meta.get("course", f"海外轉播 ({s_upper})"),
          "class_name": meta.get("class_name", "海外賽事"),
          "track_feature": meta.get("track_feature", "草地跑道"),
          "runners": runners,
      },
      bet_url,
  )


async def try_fetch_single_overseas_race(
    context: BrowserContext,
    race_date: str,
    s_code: str,
    r_no: int,
    source_mode: str | None = None,
) -> tuple[dict[str, Any] | None, str | None, str]:
  modes = [source_mode] if source_mode in {"aspx", "bet"} else ["aspx", "bet"]
  last_info = ""

  for mode in modes:
    if mode == "aspx":
      url = build_overseas_aspx_url(race_date, s_code, r_no)
      page = await context.new_page()
      try:
        await page.goto(url, wait_until="domcontentloaded", timeout=12000)
        await page.wait_for_selector("table", timeout=4000)
        runners, meta, eval_info = await parse_runners_from_dom(
            page, s_code, race_no=r_no, simulcast_code=s_code
        )
        last_info = f"{url} -> {eval_info.get('bodySnippet', '')[:160]}"
        if runners:
          return (
              {
                  "race_type": "overseas",
                  "venue_code": s_code.upper(),
                  "race_date_used": race_date,
                  "race_no": r_no,
                  "race_label": f"{s_code.upper()}-{r_no}",
                  "distance": meta.get("distance", ""),
                  "course": meta.get("course", f"海外轉播 ({s_code.upper()})"),
                  "class_name": meta.get("class_name", "海外賽事"),
                  "track_feature": meta.get("track_feature", "草地跑道"),
                  "runners": runners,
              },
              "aspx",
              url,
          )
      except Exception:
        pass
      finally:
        await page.close()
    else:
      race_obj, info = await fetch_overseas_race_via_bet_page(
          context, race_date, s_code, r_no
      )
      last_info = info
      if race_obj is not None:
        return race_obj, "bet", info

  return None, None, last_info


async def fetch_overseas_group_worker(
    sem: asyncio.Semaphore,
    context: BrowserContext,
    cand_dates: list[str],
    s_code: str,
    target_race_nos: list[int] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
  async with sem:
    group_races: list[dict[str, Any]] = []
    diag: dict[str, Any] = {"simulcast": s_code, "found": False}
    check_nos = target_race_nos if target_race_nos else list(range(1, 13))
    first_no = check_nos[0]

    matched_date = ""
    matched_mode: str | None = None
    first_runner_name = ""

    for d_str in cand_dates:
      race_obj, mode, info = await try_fetch_single_overseas_race(
          context, d_str, s_code, first_no
      )
      if race_obj is not None:
        matched_date = d_str
        matched_mode = mode
        first_runner_name = race_obj["runners"][0]["name"]
        group_races.append(race_obj)
        diag.update({
            "found": True,
            "matched_date": d_str,
            "source_mode": mode,
            "first_url": info,
        })
        break
      else:
        diag[f"probe_{d_str}"] = info

    if not matched_date:
      return [], diag

    for r_no in check_nos[1:]:
      race_obj, _, _ = await try_fetch_single_overseas_race(
          context, matched_date, s_code, r_no, source_mode=matched_mode
      )
      if race_obj is None:
        break
      if race_obj["runners"][0]["name"] == first_runner_name:
        break
      group_races.append(race_obj)

    diag["total_extracted"] = len(group_races)
    return group_races, diag


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
      for ok in ("winOdds", "currentOdds", "wpOdds", "odds"):
        if ok in node and node[ok] not in (None, "", "-", "---"):
          o_float = _extract_odds_number(node[ok])
          if o_float is not None:
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
      await page.goto(odds_url, wait_until="domcontentloaded", timeout=14000)
      await page.wait_for_timeout(700)
      if graphql_hits and len(odds_map) < len(runner_names):
        await page.wait_for_timeout(600)
    except Exception as exc:
      print(f"{race_label} odds fetch warning: {exc}")
    finally:
      page.remove_listener("response", on_resp)
      await page.close()

  if len(odds_map) < 2:
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
  t0 = time.perf_counter()

  def elapsed() -> str:
    return f"+{time.perf_counter() - t0:.1f}s"

  races: list[dict[str, Any]] = []
  debug_summary: list[dict[str, Any]] = []
  overseas_debug: list[dict[str, Any]] = []
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
      initial_url = build_local_url(
          race_date=os.getenv("HKJC_RACE_DATE", "").strip(),
          course=os.getenv("HKJC_RACE_COURSE", "").strip().upper(),
          race_no=1,
      )
      print(f"[{elapsed()}] Loading initial local racecard: {initial_url}")
      await page.goto(initial_url, wait_until="domcontentloaded", timeout=25000)
      await page.wait_for_selector("table", timeout=8000)

      race_date, course = await detect_meeting_params(page)
      print(
          f"[{elapsed()}] Detected meeting -> race_date={race_date}, local_course={course}"
      )

      local_first_runner = ""
      for race_no in range(1, max_local_races + 1):
        target_url = build_local_url(
            race_date=race_date, course=course, race_no=race_no
        )
        if race_no > 1:
          await page.goto(
              target_url, wait_until="domcontentloaded", timeout=20000
          )
          await page.wait_for_selector("table", timeout=6000)

        runners, meta, eval_info = await parse_runners_from_dom(
            page, course, race_no=race_no
        )
        if not runners:
          break
        if race_no == 1:
          local_first_runner = runners[0]["name"]
          debug_summary.append({
              "race_no": race_no,
              "url": eval_info.get("url"),
              "meta": meta,
              "runner_count": len(runners),
          })
        elif runners[0]["name"] == local_first_runner:
          break

        race_label = f"第{race_no}場"
        races.append({
            "race_type": "local",
            "venue_code": course,
            "race_date_used": race_date,
            "race_no": race_no,
            "race_label": race_label,
            "distance": meta.get("distance", ""),
            "course": meta.get("course", ""),
            "class_name": meta.get("class_name", "N/A"),
            "track_feature": meta.get("track_feature", "標準賽道"),
            "runners": runners,
        })
        print(
            f"[{elapsed()}] [Local] {race_label}: extracted {len(runners)} runners."
        )

      await page.close()
      sem = asyncio.Semaphore(concurrency)

      if include_overseas and race_date:
        cand_dates = [race_date]
        try:
          dt = datetime.strptime(race_date, "%Y/%m/%d")
          cand_dates.append((dt - timedelta(days=1)).strftime("%Y/%m/%d"))
          cand_dates.append((dt + timedelta(days=1)).strftime("%Y/%m/%d"))
        except ValueError:
          pass

        groups_to_check = (
            list(simulcast_spec.items())
            if simulcast_spec
            else [("S1", None), ("S2", None), ("S3", None), ("S4", None)]
        )
        print(
            f"[{elapsed()}] Probing overseas simulcasts {[g[0] for g in groups_to_check]} across {cand_dates}..."
        )
        group_tasks = [
            fetch_overseas_group_worker(sem, context, cand_dates, s_code, r_nos)
            for s_code, r_nos in groups_to_check
        ]
        group_results = await asyncio.gather(*group_tasks)

        seen_overseas_sigs: set[tuple[str, int]] = set()
        for g_races, g_diag in group_results:
          overseas_debug.append(g_diag)
          if not g_races:
            continue
          sig = (g_races[0]["runners"][0]["name"], len(g_races[0]["runners"]))
          if sig in seen_overseas_sigs:
            continue
          seen_overseas_sigs.add(sig)
          for item in g_races:
            races.append(item)
            print(
                f"[{elapsed()}] [Overseas] {item['race_label']} ({item['race_date_used']}): extracted {len(item['runners'])} runners."
            )

      races_needing_odds = [
          r_obj
          for r_obj in races
          if not any(r.get("win_odds") is not None for r in r_obj["runners"])
      ]
      if race_date and races_needing_odds:
        print(
            f"[{elapsed()}] Fetching live win odds concurrently for {len(races_needing_odds)} races (workers={concurrency})..."
        )
        odds_tasks = [
            fetch_win_odds_worker(
                sem,
                context,
                race_obj.get("race_date_used") or race_date,
                race_obj["venue_code"],
                race_obj["race_no"],
                race_obj["race_label"],
                {r["no"]: r["name"] for r in race_obj["runners"]},
            )
            for race_obj in races_needing_odds
        ]
        odds_results = await asyncio.gather(*odds_tasks)
        odds_by_label = {lbl: o_map for lbl, o_map, _ in odds_results}
        odds_diagnostics = [diag for _, _, diag in odds_results]

        for race_obj in races_needing_odds:
          lbl = race_obj["race_label"]
          o_map = odds_by_label.get(lbl, {})
          for r in race_obj["runners"]:
            if r["no"] in o_map:
              r["win_odds"] = o_map[r["no"]]
    finally:
      await browser.close()

  RAW_DEBUG_PATH.parent.mkdir(parents=True, exist_ok=True)
  RAW_DEBUG_PATH.write_text(
      json.dumps(
          {
              "total_races": len(races),
              "elapsed_seconds": round(time.perf_counter() - t0, 2),
              "racecard_summary": debug_summary,
              "overseas_debug": overseas_debug,
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
  print(
      f"[{elapsed()}] Saved {len(races)} total races to {OUTPUT_DATA_PATH}"
  )
  return result


def fetch_hkjc_race_data() -> dict[str, Any]:
  return asyncio.run(fetch_hkjc_race_data_async())


if __name__ == "__main__":
  fetch_hkjc_race_data()
