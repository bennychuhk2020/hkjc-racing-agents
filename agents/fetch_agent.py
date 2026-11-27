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
            bw_display = raw_bw_cell if "(" in raw_bw_cell else str(body_weight)

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
      best_time = (
          row[col_map["best_time"]].strip() or "--"
          if "best_time" in col_map
          else "--"
      )
      gear = (
          row[col_map["gear"]].strip() or "--"
          if "gear" in col_map
          else "--"
      )
      trump_raw = (
          row[col_map["trump_card"]].strip()
          if "trump_card" in col_map
          else ""
      )
      trump_card = "王牌" if trump_raw and trump_raw not in {"-", "--"} else "未使用"

      priority_raw = (
          row[col_map["priority_entry"]].strip()
          if "priority_entry" in col_map
          else ""
      )
      priority_entry = (
          "獲得" if priority_raw and priority_raw not in {"-", "--"} else "一般"
      )

      trainer_pref = (
          row[col_map["trainer_pref"]].strip().replace(" ", "") or "--"
          if "trainer_pref" in col_map
          else "--"
      )

      comment = (
          comments_by_no.get(horse_no)
          or comments_by_name.get(horse_name)
          or "N/A"
      )

      running_style = infer_running_style(
          recent_runs, draw, meta.get("distance", "1400")
      )

      seen_nos.add(horse_no)
      runners.append({
          "no": horse_no,
          "name": horse_name,
          "horse_url": horse_url,
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
          "best_time": best_time,
          "gear": gear,
          "trump_card": trump_card,
          "priority_entry": priority_entry,
          "trainer_pref": trainer_pref,
          "comment": comment,
          "age_sex": "N/A",
          "season_stats": "N/A",
          "days_since_last": "N/A",
          "trackwork_summary": "N/A",
          "win_odds": None,
      })

    if len(runners) >= 4:
      break

  return runners, meta, eval_result


def fetch_race_comments(
    page, race_date: str, course: str, race_no: int
) -> dict[str, str]:
  """嘗試從 HKJC 排位評語頁補充抓出每匹馬嘅賽前評語。"""
  url = (
      f"https://racing.hkjc.com/zh-hk/local/information/racing-comment"
      f"?raceDate={race_date}&Racecourse={course}&RaceNo={race_no}"
  )
  comments: dict[str, str] = {}
  try:
    page.goto(url, wait_until="domcontentloaded", timeout=25000)
    page.wait_for_timeout(1500)
    rows = page.evaluate("""
      () => Array.from(document.querySelectorAll("table tr")).map(tr =>
        Array.from(tr.querySelectorAll("th, td")).map(c => c.innerText.replace(/\\s+/g, " ").trim())
      )
    """)
    for r in rows:
      if len(r) >= 3 and re.fullmatch(r"\d{1,2}", r[0]):
        h_name = r[1].strip()
        cmt = r[-1].strip()
        if h_name and len(cmt) > 5 and cmt != h_name:
          comments[h_name] = cmt
  except Exception:
    pass
  return comments


def enrich_horse_profile(
    page, horse_url: str, target_race_date: str
) -> dict[str, Any]:
  """精準解析馬匹資料頁：年齡/性別、歷季出賽、上次馬匹體重、真實距上次出賽日數及晨操試閘。"""
  if not horse_url:
    return {}

  try:
    race_dt = datetime.strptime(target_race_date, "%Y/%m/%d").replace(tzinfo=HKT)
  except ValueError:
    race_dt = datetime.now(HKT)

  try:
    page.goto(horse_url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(1200)

    data = page.evaluate("""
      () => {
        const bodyText = document.body ? document.body.innerText : "";
        const rows = Array.from(document.querySelectorAll("table tr")).map(tr =>
          Array.from(tr.querySelectorAll("th, td")).map(c => c.innerText.replace(/\\s+/g, " ").trim())
        );
        const trackworkLink = Array.from(document.querySelectorAll("a")).find(a =>
          (a.innerText || "").includes("晨操") || (a.href || "").toLowerCase().includes("trackwork")
        );
        return {
          bodyText,
          rows,
          trackworkUrl: trackworkLink ? trackworkLink.href : ""
        };
      }
    """)

    out: dict[str, Any] = {}
    rows: list[list[str]] = data.get("rows", [])
    body_text: str = data.get("bodyText", "")

    # 1. 從資料表格鍵值對精準提取「年齡 / 性別」同「冠-亞-季-總出賽次數」
    for r in rows:
      for idx, cell in enumerate(r):
        clean_k = cell.replace(" ", "")
        val = r[idx + 1].strip() if idx + 1 < len(r) else ""
        if not val and ":" in cell:
          val = cell.split(":", 1)[1].strip()
        if not val and "：" in cell:
          val = cell.split("：", 1)[1].strip()

        if "年齡/性別" in clean_k and val:
          out["age_sex"] = val
        elif ("冠-亞-季-總出賽次數" in clean_k or "歷季出賽" in clean_k) and val:
          parts = re.findall(r"\d+", val)
          if len(parts) >= 4:
            # 轉成 "21 (0-3-0)" 格式
            out["season_stats"] = f"{parts[3]} ({parts[0]}-{parts[1]}-{parts[2]})"
          else:
            out["season_stats"] = val

    # 後備正則掃描純文字（應對無表格布局）
    if "age_sex" not in out:
      m_age = re.search(r"年齡\s*/\s*性別\s*[:：]?\s*(\d+\s*/\s*[閹雄雌公母])", body_text)
      if m_age:
        out["age_sex"] = m_age.group(1).strip()

    if "season_stats" not in out:
      m_st = re.search(r"冠-亞-季-總出賽次數\s*[:：]?\s*(\d+-\d+-\d+-\d+)", body_text)
      if m_st:
        p = m_st.group(1).split("-")
        out["season_stats"] = f"{p[3]} ({p[0]}-{p[1]}-{p[2]})"

    # 2. 從往績列尋找最近一場「嚴格早於賽日」嘅出賽日期與該場體重
    past_dates: list[datetime] = []
    last_weight_from_history: int | None = None

    for r in rows:
      row_joined = " ".join(r)
      m_d = re.search(r"\b(\d{2})/(\d{2})/(\d{2,4})\b", row_joined)
      if not m_d:
        continue
      d_s, m_s, y_s = m_d.groups()
      yr = int(y_s) + 2000 if len(y_s) == 2 else int(y_s)
      try:
        dt = datetime(yr, int(m_s), int(d_s), tzinfo=HKT)
      except ValueError:
        continue

      # 必須早於今次賽日（排除排位日期及未來報名），且喺合理 2 年內
      diff_days = (race_dt - dt).days
      if 2 <= diff_days <= 730:
        if not past_dates:
          # 第一筆匹配通常為最近一場往績，順手抓取該場馬匹體重 (900-1380 磅)
          for cell in r:
            if re.fullmatch(r"\d{4}", cell.strip()):
              w_cand = int(cell.strip())
              if 900 <= w_cand <= 1380:
                last_weight_from_history = w_cand
                break
        past_dates.append(dt)

    if past_dates:
      latest_run = max(past_dates)
      days_diff = (race_dt - latest_run).days
      out["days_since_last"] = f"{days_diff}日"

    if last_weight_from_history is not None:
      out["last_run_weight"] = last_weight_from_history

    # 3. 進入馬匹專屬「晨操及試閘」頁面統計真實次數
    tw_url = data.get("trackworkUrl", "")
    if tw_url:
      try:
        page.goto(tw_url, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(1000)
        tw_rows = page.evaluate("""
          () => Array.from(document.querySelectorAll("table tr")).map(tr => tr.innerText)
        """)
        # 只統計包含日期格式（如 24/11/2026）嘅真實操練紀錄列
        valid_records = [
            row_t for row_t in tw_rows if re.search(r"\d{2}/\d{2}/\d{2,4}", row_t)
        ]
        tw_counts = {
            "試閘": sum(1 for x in valid_records if "試閘" in x),
            "快操": sum(1 for x in valid_records if "快操" in x or "彈閘" in x),
            "踱步": sum(1 for x in valid_records if "踱步" in x),
            "游水": sum(1 for x in valid_records if "游水" in x),
        }
        parts = [f"{k}:{v}" for k, v in tw_counts.items() if v > 0]
        out["trackwork_summary"] = " ".join(parts) if parts else "無近期紀錄"
      except Exception:
        pass

    return out
  except Exception:
    return {}


def extract_odds_from_json_tree(node: Any, odds_map: dict[int, float]) -> None:
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
  fetch_horse_profiles = os.getenv("HKJC_FETCH_HORSE_DETAILS", "true").lower() == "true"

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

        # 補充抓取賽前評語頁面
        if race_date and any(r["comment"] == "N/A" for r in runners):
          extra_comments = fetch_race_comments(page, race_date, course, race_no)
          for r in runners:
            if r["comment"] == "N/A" and r["name"] in extra_comments:
              r["comment"] = extra_comments[r["name"]]

        races.append({
            "race_no": race_no,
            "distance": meta.get("distance", ""),
            "course": meta.get("course", ""),
            "class_name": meta.get("class_name", "N/A"),
            "track_feature": meta.get("track_feature", "標準賽道"),
            "runners": runners,
        })
        print(f"Race {race_no}: extracted {len(runners)} runners.")

      if fetch_horse_profiles and races:
        print("Enriching horse profile details (age/sex, stats, weight, trackwork)...")
        for race_obj in races:
          for r in race_obj["runners"]:
            extra = enrich_horse_profile(
                page, r.get("horse_url", ""), race_date
            )
            # 若排位表尚未公佈體重，改用往績最近一場體重作參考標註
            if r.get("body_weight") == "N/A" and "last_run_weight" in extra:
              r["body_weight"] = extra["last_run_weight"]
              r["bw_display"] = f"{extra['last_run_weight']} (上仗)"
            r.update({k: v for k, v in extra.items() if k != "last_run_weight"})

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
