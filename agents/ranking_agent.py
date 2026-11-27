from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

DATA_PATH = Path("data/upcoming_races.json")
OUT_JSON = Path("output/predictions.json")
OUT_MD = Path("output/report.md")

TOP_JOCKEYS = ["潘頓", "布文", "田泰安", "鍾易禮", "艾兆禮", "巴度", "何澤堯"]
TOP_TRAINERS = ["伍鵬志", "呂健威", "告東尼", "蔡約翰", "方嘉柏", "沈集成", "羅富全"]


def calc_draw_score(draw_val: Any, track_feature: str) -> float:
  try:
    d_num = int(float(draw_val))
  except (ValueError, TypeError):
    return 50.0

  is_tight_track = any(k in track_feature for k in ("C+3", "C", "B+2"))
  is_a_track = ("A" in track_feature) and ("+" not in track_feature)

  if is_tight_track:
    if d_num <= 3:
      return 90.0
    if d_num <= 7:
      return 70.0
    return 35.0
  if is_a_track:
    if d_num <= 2:
      return 65.0
    if 3 <= d_num <= 8:
      return 88.0
    if 9 <= d_num <= 11:
      return 75.0
    return 50.0
  if d_num <= 4:
    return 80.0
  if d_num <= 9:
    return 70.0
  return 50.0


def calc_style_score(running_style: str, track_feature: str) -> float:
  is_tight_track = any(k in track_feature for k in ("C+3", "C", "B+2"))
  is_a_track = ("A" in track_feature) and ("+" not in track_feature)

  if is_tight_track:
    if running_style == "領放":
      return 92.0
    if running_style == "前置機動":
      return 86.0
    if running_style == "居中順走":
      return 72.0
    return 58.0

  if is_a_track:
    if running_style in {"居中順走", "後上", "前置機動"}:
      return 85.0
    return 74.0

  return 76.0


def calc_form_score(recent_runs: str, rating_val: Any) -> float:
  if recent_runs and recent_runs != "N/A":
    runs = re.findall(r"\b\d+\b", recent_runs.replace("/", " "))
    if runs:
      weights = [6, 5, 4, 3, 2, 1]
      weighted_sum = 0.0
      total_weight = 0
      for idx, pos_str in enumerate(runs[:6]):
        pos = int(pos_str)
        w = weights[idx]
        if pos == 1:
          p_score = 100
        elif pos == 2:
          p_score = 85
        elif pos == 3:
          p_score = 75
        elif pos <= 5:
          p_score = 55
        else:
          p_score = 25
        weighted_sum += p_score * w
        total_weight += w
      if total_weight > 0:
        return round(weighted_sum / total_weight, 1)

  try:
    rtg = float(rating_val)
    return round(max(40.0, min(85.0, 45.0 + (rtg - 40.0) * 0.6)), 1)
  except (ValueError, TypeError):
    return 55.0


def calc_jt_score(jockey_val: str, trainer_val: str) -> float:
  score = 60.0
  if any(tj in jockey_val for tj in TOP_JOCKEYS):
    score += 20.0
  if any(tt in trainer_val for tt in TOP_TRAINERS):
    score += 15.0
  return score


def calc_body_weight(body_weight: Any) -> tuple[str, str, float]:
  """回傳 (排位體重顯示字串, 體重狀態, 體重分數)。"""
  try:
    bw = int(float(body_weight))
    if bw <= 0:
      return "N/A", "待公佈", 65.0
    if bw < 1020:
      return str(bw), "體型偏瘦", 55.0
    if bw > 1220:
      return str(bw), "身材魁梧", 75.0
    return str(bw), "黃金標準", 80.0
  except (ValueError, TypeError):
    return "N/A", "待公佈", 65.0


def evaluate_value(cpi: float, odds_val: float | None) -> str:
  if odds_val is None or odds_val <= 1.0:
    return "⏳ 待開盤"
  if cpi >= 70.0 and odds_val >= 8.0:
    return "💎 超值冷門"
  if cpi >= 74.0 and odds_val < 3.0:
    return "🔥 順熱熱門"
  if cpi < 55.0 and odds_val < 4.0:
    return "⚠️ 賠率過熱"
  return "⚖️ 賠率合理"


def analyze_race(race: dict[str, Any]) -> dict[str, Any]:
  race_no = race.get("race_no", 0)
  distance = str(race.get("distance", "N/A"))
  course = str(race.get("course", "沙田"))
  class_name = str(race.get("class_name", "N/A"))
  track_feature = str(race.get("track_feature", "標準賽道"))

  analyzed: list[dict[str, Any]] = []
  for idx, r in enumerate(race.get("runners", [])):
    horse_no = r.get("no", idx + 1)
    horse_name = str(r.get("name", "未知"))
    draw_val = r.get("draw", "N/A")
    weight_val = r.get("weight_lbs", "N/A")
    rating_val = r.get("rating", "N/A")
    jockey_val = str(r.get("jockey", "N/A"))
    trainer_val = str(r.get("trainer", "N/A"))
    recent_runs = str(r.get("recent_runs", "N/A"))
    running_style = str(r.get("running_style", "前置機動"))
    raw_bw = r.get("body_weight", "N/A")

    raw_odds = r.get("win_odds")
    odds_val: float | None = None
    if raw_odds not in (None, "", "N/A"):
      try:
        parsed = float(raw_odds)
        if parsed >= 1.1:
          odds_val = parsed
      except (ValueError, TypeError):
        odds_val = None

    odds_display = f"{odds_val:.1f}" if odds_val is not None else "N/A"

    draw_score = calc_draw_score(draw_val, track_feature)
    style_score = calc_style_score(running_style, track_feature)
    form_score = calc_form_score(recent_runs, rating_val)
    jt_score = calc_jt_score(jockey_val, trainer_val)
    bw_display, weight_health, bw_score = calc_body_weight(raw_bw)

    cpi = round(
        (form_score * 0.30)
        + (draw_score * 0.25)
        + (style_score * 0.20)
        + (jt_score * 0.15)
        + (bw_score * 0.10),
        1,
    )
    value_status = evaluate_value(cpi, odds_val)

    analyzed.append({
        "馬號": horse_no,
        "馬名": horse_name,
        "綜合馬力指數(CPI)": cpi,
        "獨贏賠率": odds_display,
        "值博率評估": value_status,
        "跑法": running_style,
        "檔位": draw_val,
        "負磅": weight_val,
        "現時評分": rating_val,
        "排位體重": bw_display,
        "體重狀態": weight_health,
        "騎師": jockey_val,
        "練馬師": trainer_val,
    })

  analyzed.sort(key=lambda x: x["綜合馬力指數(CPI)"], reverse=True)
  return {
      "race_no": race_no,
      "course": course,
      "class_name": class_name,
      "distance": distance,
      "track_feature": track_feature,
      "runners": analyzed,
  }


def run_analysis() -> None:
  if not DATA_PATH.exists():
    print("No race data found.")
    return

  raw = json.loads(DATA_PATH.read_text(encoding="utf-8"))
  race_date = raw.get("race_date", "N/A")
  fetched_at = raw.get("fetched_at_hkt", "N/A")
  races = raw.get("races", [])

  all_analyzed = [analyze_race(race) for race in races]

  md_lines = [
      "# HKJC 多代理 (Multi-Agent) 賽事預測系統",
      f"**比賽日期：** {race_date} ｜ **更新時間 (HKT)：** `{fetched_at}` ｜ **已完成場次：** 第 1 至 {len(all_analyzed)} 場\n",
  ]

  for r in all_analyzed:
    dist_txt = (
        f"{r['distance']}米" if str(r["distance"]).isdigit() else r["distance"]
    )
    md_lines.append(f"## ※第{r['race_no']}場")
    md_lines.append(
        f"**賽事元數據：** 馬場 `{r['course']}` ｜ 場次 `第{r['race_no']}場` ｜ "
        f"班次 `{r['class_name']}` ｜ 途程 `{dist_txt}` ｜ 跑道特色 `{r['track_feature']}`\n"
    )
    md_lines.append(
        "| 馬號 | 馬名 | 綜合馬力指數(CPI) | 獨贏賠率 | 值博率評估 | 跑法 | 檔位 | 負磅 | 現時評分 | 排位體重 | 體重狀態 | 騎師 | 練馬師 |"
    )
    md_lines.append(
        "| :---: | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :--- | :--- |"
    )
    for h in r["runners"]:
      md_lines.append(
          f"| {h['馬號']} | **{h['馬名']}** | **{h['綜合馬力指數(CPI)']}** | "
          f"{h['獨贏賠率']} | {h['值博率評估']} | {h['跑法']} | {h['檔位']} | {h['負磅']} | "
          f"{h['現時評分']} | {h['排位體重']} | {h['體重狀態']} | {h['騎師']} | {h['練馬師']} |"
      )
    md_lines.append("\n---\n")

  OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
  OUT_JSON.write_text(
      json.dumps(
          {
              "race_date": race_date,
              "fetched_at_hkt": fetched_at,
              "races": all_analyzed,
          },
          ensure_ascii=False,
          indent=2,
      ),
      encoding="utf-8",
  )

  OUT_MD.parent.mkdir(parents=True, exist_ok=True)
  OUT_MD.write_text("\n".join(md_lines), encoding="utf-8")
  print(f"Generated report for {len(all_analyzed)} races.")


if __name__ == "__main__":
  run_analysis()
