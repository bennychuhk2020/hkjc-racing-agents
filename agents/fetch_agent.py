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
  """解析排位表主表、馬匹資料連結及賽前評語。"""
  eval_result = page.evaluate("""
    () => {
      const title = document.title || "";
      const bodyText = document.body ? document.body.innerText : "";
      const tables = Array.from(document.querySelectorAll("table"));
      const extractedTables = tables.map((tbl, idx) => {
        const rows = Array.from(tbl.querySelectorAll("tr")).map(tr => {
          const cells = Array.from(tr.querySelectorAll("th, td"));
          const texts = cells.map(cell => cell.innerText.replace(/\\s+/g, " ").trim());
          const links = cells.map(cell => {
            const a = cell.querySelector("a[href*='Horse'], a[href*='horse']");
            return a ? a.href : "";
          });
          return { texts, links };
        });
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

  # 1. 提取頁面內任何包含「評語」嘅表格
  comments_by_no: dict[int, str] = {}
  comments_by_name: dict[str, str] = {}
  for tbl in eval_result.get("tables", []):
    rows = tbl.get("rows", [])
    if len(rows) < 3:
      continue
    header_texts = rows[0]["texts"]
    joined_h = " ".join(header_texts)
    if "評語" in joined_h:
      c_idx = next((i for i, h in enumerate(header_texts) if "評語" in h), -1)
      n_idx = next((i for i, h in enumerate(header_texts) if "馬名" in h), -1)
      no_idx = next((i for i, h in enumerate(header_texts) if any(k in h for k in ("馬號", "編號", "馬匹編號"))), -1)
      for r in rows[1:]:
        txts = r["texts"]
        if c_idx != -1 and len(txts) > c_idx and len(txts[c_idx].strip()) > 4:
          cmt = txts[c_idx].strip()
          if no_idx != -1 and len(txts) > no_idx and re.fullmatch(r"\d{1,2}", txts[no_idx].strip()):
            comments_by_no[int(txts[no_idx].strip())] = cmt
          if n_idx != -1 and len(txts) > n_idx and txts[n_idx].strip():
            comments_by_name[txts[n_idx].strip()] = cmt

  # 2. 提取排位主表
  for tbl in eval_result.get("tables", []):
    rows = tbl.get("rows", [])
    if len(rows) < 5:
      continue

    header_idx = -1
    col_map: dict[str, int] = {}

    for r_idx, row_obj in enumerate(rows[:4]):
      row = row_obj["texts"]
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
          elif "最佳時間" in clean_col:
            col_map["best_time"] = c_idx
          elif "配備" in clean_col:
            col_map["gear"] = c_idx
          elif "王牌" in clean_col:
            col_map["trump_card"] = c_idx
          elif "優先" in clean_col and "次序" not in clean_col:
            col_map["priority_entry"] = c_idx
          elif "次序" in clean_col or "報名" in clean_col:
            col_map["trainer_pref"] = c_idx
        break

    if header_idx == -1 or "no" not in col_map or "name" not in col_map:
      continue

    seen_nos: set[int] = set()
    for row_obj in rows[header_idx + 1 :]:
      row = row_obj["texts"]
      links = row_obj["links"]
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

      horse_url = links[col_map["name"]] if len(links) > col_map["name"] else ""

      weight_lbs = (
          safe_float(row[col_map["weight"]], 125.0)
          if "weight" in col_map
          else 125.0
      )
      if weight_lbs is None or not (100.0 <= weight_lbs <= 140.0):
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
      if not (1 <= draw <= 16):
        draw = 7

      rating = (
          safe_float(row[col_map["rating"]], 60.0)
          if "rating" in col_map
          else 60.0
      )
      if rating is None or not (10.0 <= rating <= 150.0):
        rating = 60.0

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
      bw_
