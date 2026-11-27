from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Any

from weasyprint import HTML

DATA_PATH = Path("data/upcoming_races.json")
OUT_JSON = Path("output/predictions.json")
OUT_MD = Path("output/report.md")
OUT_PDF = Path("output/report.pdf")

TOP_JOCKEYS = frozenset({"潘頓", "布文", "田泰安", "鍾易禮", "艾兆禮", "巴度", "何澤堯"})
TOP_TRAINERS = frozenset({"伍鵬志", "呂健威", "告東尼", "蔡約翰", "方嘉柏", "沈集成", "羅富全"})

POS_SCORE_MAP = {1: 100.0, 2: 85.0, 3: 75.0, 4: 55.0, 5: 55.0}
RUN_WEIGHTS = (6, 5, 4, 3, 2, 1)


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
      weighted_sum = 0.0
      total_weight = 0
      for pos_str, w in zip(runs[:6], RUN_WEIGHTS):
        p_score = POS_SCORE_MAP.get(int(pos_str), 25.0)
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
  if jockey_val in TOP_JOCKEYS or any(tj in jockey_val for tj in TOP_JOCKEYS):
    score += 20.0
  if trainer_val in TOP_TRAINERS or any(tt in trainer_val for tt in TOP_TRAINERS):
    score += 15.0
  return score


def calc_body_weight(body_weight: Any, bw_display: str) -> tuple[str, str, float]:
  try:
    bw = int(float(body_weight))
    if bw <= 0:
      return "N/A", "待公佈", 65.0
    disp = bw_display if bw_display and bw_display != "N/A" else str(bw)
    if bw < 1020:
      return disp, "體型偏瘦", 55.0
    if bw > 1220:
      return disp, "身材魁梧", 75.0
    return disp, "黃金標準", 80.0
  except (ValueError, TypeError):
    return "N/A", "待公佈", 65.0


def evaluate_value(hri: float, odds_val: float | None) -> str:
  if odds_val is None or odds_val <= 1.0:
    return "⏳ 待開盤"
  if hri >= 70.0 and odds_val >= 8.0:
    return "💎 超值冷門"
  if hri >= 74.0 and odds_val < 3.0:
    return "🔥 順熱熱門"
  if hri < 55.0 and odds_val < 4.0:
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
    weight_disp = str(r.get("weight_display") or weight_val)
    rating_val = r.get("rating", "N/A")
    rating_disp = str(r.get("rating_display") or rating_val)
    jockey_val = str(r.get("jockey", "N/A"))
    trainer_val = str(r.get("trainer", "N/A"))
    recent_runs = str(r.get("recent_runs", "N/A"))
    running_style = str(r.get("running_style", "前置機動"))
    raw_bw = r.get("body_weight", "N/A")
    raw_bw_disp = str(r.get("bw_display", "N/A"))

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
    bw_display, weight_health, bw_score = calc_body_weight(raw_bw, raw_bw_disp)

    hri = round(
        (form_score * 0.30)
        + (draw_score * 0.25)
        + (style_score * 0.20)
        + (jt_score * 0.15)
        + (bw_score * 0.10),
        1,
    )
    value_status = evaluate_value(hri, odds_val)

    analyzed.append({
        "馬號": horse_no,
        "馬名": horse_name,
        "綜合指數(HRI)": hri,
        "獨贏賠率": odds_display,
        "值博率評估": value_status,
        "跑法": running_style,
        "檔位": draw_val,
        "負磅": weight_disp,
        "現時評分": rating_disp,
        "排位體重": bw_display,
        "體重狀態": weight_health,
        "騎師": jockey_val,
        "練馬師": trainer_val,
        "6次近績": recent_runs,
    })

  analyzed.sort(key=lambda x: x["綜合指數(HRI)"], reverse=True)
  return {
      "race_no": race_no,
      "course": course,
      "class_name": class_name,
      "distance": distance,
      "track_feature": track_feature,
      "runners": analyzed,
  }


def build_pdf_report(
    race_date: str,
    fetched_at: str,
    all_analyzed: list[dict[str, Any]],
    headers: list[str],
    output_pdf: Path,
) -> None:
  """Render a clean landscape A4 PDF report with Traditional Chinese CJK fonts."""
  race_sections: list[str] = []

  for r in all_analyzed:
    dist_txt = (
        f"{r['distance']}米" if str(r["distance"]).isdigit() else str(r["distance"])
    )
    th_html = "".join(f"<th>{html.escape(h)}</th>" for h in headers)

    rows_html: list[str] = []
    for idx, h in enumerate(r["runners"]):
      row_cls = "top-pick" if idx < 3 else ""
      cells = [
          f"<td>{html.escape(str(h['馬號']))}</td>",
          f"<td class='horse-name'>{html.escape(str(h['馬名']))}</td>",
          f"<td class='hri-score'>{html.escape(str(h['綜合指數(HRI)']))}</td>",
          f"<td>{html.escape(str(h['獨贏賠率']))}</td>",
          f"<td>{html.escape(str(h['值博率評估']))}</td>",
          f"<td>{html.escape(str(h['跑法']))}</td>",
          f"<td>{html.escape(str(h['檔位']))}</td>",
          f"<td>{html.escape(str(h['負磅']))}</td>",
          f"<td>{html.escape(str(h['現時評分']))}</td>",
          f"<td>{html.escape(str(h['排位體重']))}</td>",
          f"<td>{html.escape(str(h['體重狀態']))}</td>",
          f"<td>{html.escape(str(h['騎師']))}</td>",
          f"<td>{html.escape(str(h['練馬師']))}</td>",
          f"<td>{html.escape(str(h['6次近績']))}</td>",
      ]
      rows_html.append(f"<tr class='{row_cls}'>{''.join(cells)}</tr>")

    race_sections.append(f"""
    <section class="race-block">
      <div class="race-header">
        <span class="race-title">※ 第 {html.escape(str(r['race_no']))} 場</span>
        <span class="meta-pill">馬場：<b>{html.escape(str(r['course']))}</b></span>
        <span class="meta-pill">班次：<b>{html.escape(str(r['class_name']))}</b></span>
        <span class="meta-pill">途程：<b>{html.escape(dist_txt)}</b></span>
        <span class="meta-pill">跑道特色：<b>{html.escape(str(r['track_feature']))}</b></span>
      </div>
      <table>
        <thead><tr>{th_html}</tr></thead>
        <tbody>{''.join(rows_html)}</tbody>
      </table>
    </section>
    """)

  full_html = f"""<!DOCTYPE html>
<html lang="zh-HK">
<head>
<meta charset="utf-8" />
<style>
  @page {{
    size: A4 landscape;
    margin: 10mm 10mm 12mm 10mm;
    @bottom-right {{
      content: "第 " counter(page) " / " counter(pages) " 頁";
      font-family: "Noto Sans CJK HK", "Noto Sans CJK TC", sans-serif;
      font-size: 8pt;
      color: #64748b;
    }}
  }}
  body {{
    font-family: "Noto Sans CJK HK", "Noto Sans CJK TC", "Microsoft JhengHei", sans-serif;
    color: #0f172a;
    margin: 0;
    padding: 0;
    font-size: 8.5pt;
  }}
  .doc-banner {{
    background: linear-gradient(135deg, #0f172a 0%, #1e3a8a 100%);
    color: #ffffff;
    padding: 10px 14px;
    border-radius: 6px;
    margin-bottom: 12px;
  }}
  .doc-banner h1 {{
    margin: 0 0 4px 0;
    font-size: 14pt;
    letter-spacing: 0.5px;
  }}
  .doc-banner .sub {{
    font-size: 8.5pt;
    color: #cbd5e1;
  }}
  .race-block {{
    margin-bottom: 14px;
    page-break-inside: avoid;
  }}
  .race-header {{
    background-color: #f1f5f9;
    border-left: 4px solid #1d4ed8;
    padding: 6px 10px;
    margin-bottom: 5px;
    border-radius: 0 4px 4px 0;
  }}
  .race-title {{
    font-size: 10.5pt;
    font-weight: bold;
    color: #1e3a8a;
    margin-right: 12px;
  }}
  .meta-pill {{
    display: inline-block;
    background: #ffffff;
    border: 1px solid #cbd5e1;
    border-radius: 4px;
    padding: 1px 7px;
    margin-right: 6px;
    font-size: 8pt;
    color: #334155;
  }}
  table {{
    width: 100%;
    border-collapse: collapse;
    table-layout: fixed;
  }}
  th {{
    background-color: #1e293b;
    color: #ffffff;
    font-weight: 600;
    padding: 5px 3px;
    text-align: center;
    border: 1px solid #1e293b;
    font-size: 7.8pt;
    word-wrap: break-word;
  }}
  td {{
    padding: 4px 3px;
    text-align: center;
    border: 1px solid #cbd5e1;
    font-size: 8pt;
    word-wrap: break-word;
  }}
  tbody tr:nth-child(even) {{
    background-color: #f8fafc;
  }}
  tbody tr.top-pick {{
    background-color: #eff6ff;
  }}
  td.horse-name {{
    font-weight: 700;
    color: #0f172a;
  }}
  td.hri-score {{
    font-weight: 700;
    color: #b91c1c;
    font-size: 8.5pt;
  }}
</style>
</head>
<body>
  <div class="doc-banner">
    <h1>HKJC 多代理 (Multi-Agent) 賽事預測系統 — 綜合指數 (HRI) 戰報</h1>
    <div class="sub">
      比賽日期：<b>{html.escape(race_date)}</b> ｜ 已完成場次：第 1 至 {len(all_analyzed)} 場 ｜ 更新時間 (HKT)：{html.escape(fetched_at)}
    </div>
  </div>
  {''.join(race_sections)}
</body>
</html>
"""

  output_pdf.parent.mkdir(parents=True, exist_ok=True)
  HTML(string=full_html).write_pdf(str(output_pdf))


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

  headers = [
      "馬號", "馬名", "綜合指數(HRI)", "獨贏賠率", "值博率評估",
      "跑法", "檔位", "負磅", "現時評分", "排位體重", "體重狀態",
      "騎師", "練馬師", "6次近績"
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
    md_lines.append("| " + " | ".join(headers) + " |")
    md_lines.append("| " + " | ".join([":---:"] * len(headers)) + " |")
    for h in r["runners"]:
      row_vals = [
          str(h["馬號"]),
          f"**{h['馬名']}**",
          f"**{h['綜合指數(HRI)']}**",
          str(h["獨贏賠率"]),
          str(h["值博率評估"]),
          str(h["跑法"]),
          str(h["檔位"]),
          str(h["負磅"]),
          str(h["現時評分"]),
          str(h["排位體重"]),
          str(h["體重狀態"]),
          str(h["騎師"]),
          str(h["練馬師"]),
          str(h["6次近績"]),
      ]
      md_lines.append("| " + " | ".join(row_vals) + " |")
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

  build_pdf_report(race_date, fetched_at, all_analyzed, headers, OUT_PDF)
  print(f"Generated Markdown, JSON, and PDF report ({OUT_PDF}) for {len(all_analyzed)} races.")


if __name__ == "__main__":
  run_analysis()
