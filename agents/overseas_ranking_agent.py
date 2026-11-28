from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

INPUT_DATA_PATH = Path("data/upcoming_races.json")
OUTPUT_RANKED_PATH = Path("data/ranked_overseas_races.json")


def _to_float(val: Any, default: float | None = None) -> float | None:
  if val is None:
    return default
  try:
    f = float(val)
    return f if math.isfinite(f) else default
  except (TypeError, ValueError):
    return default


def score_class_tier(class_name: str) -> float:
  text = str(class_name or "")
  if "一級賽" in text or "G1" in text.upper():
    return 95.0
  if "二級賽" in text or "G2" in text.upper():
    return 88.0
  if "三級賽" in text or "G3" in text.upper():
    return 82.0
  if "表列賽" in text or "公開賽" in text:
    return 76.0
  return 68.0


def score_weight_and_draw(
    weight_lbs: float,
    min_weight: float,
    max_weight: float,
    draw: int,
    field_size: int,
    distance_m: int,
) -> float:
  # 相對負磅表現：高負磅代表實力受肯定，但過重（如 >133磅）在長途略扣分
  spread = max(1.0, max_weight - min_weight)
  rel_weight = (weight_lbs - min_weight) / spread  # 0.0 ~ 1.0
  weight_score = 55.0 + rel_weight * 35.0
  if weight_lbs > 133.0:
    weight_score -= min(10.0, (weight_lbs - 133.0) * 2.0)

  # 檔位適應度：按途程調整內外檔偏好
  safe_field = max(6, field_size)
  draw_ratio = (draw - 1) / max(1, safe_field - 1)  # 0.0 (最內檔) ~ 1.0 (最外檔)

  if distance_m <= 1400:
    # 短途較偏好內中檔
    draw_score = 90.0 - abs(draw_ratio - 0.30) * 45.0
  elif distance_m <= 1800:
    # 一哩至中距離較均衡，略偏中內檔
    draw_score = 88.0 - abs(draw_ratio - 0.40) * 35.0
  else:
    # 長途（2000米以上）較少受單一檔位限制
    draw_score = 84.0 - abs(draw_ratio - 0.50) * 20.0

  draw_score = max(45.0, min(95.0, draw_score))
  return round(0.60 * weight_score + 0.40 * draw_score, 2)


def score_pace_fit(running_style: str, draw: int, distance_m: int) -> float:
  style = str(running_style or "前置機動")
  base_map = {
      "領放": 76.0,
      "前置機動": 84.0,
      "居中順走": 80.0,
      "後上": 72.0,
  }
  score = base_map.get(style, 76.0)

  if style == "領放" and distance_m <= 1400 and draw <= 5:
    score += 10.0
  elif style == "前置機動" and draw <= 7:
    score += 6.0
  elif style == "後上" and distance_m >= 1800:
    score += 10.0

  return round(max(45.0, min(96.0, score)), 2)


def evaluate_value_bet(
    model_prob: float, win_odds: float | None
) -> tuple[str, float | None]:
  if win_odds is None or win_odds <= 1.0:
    return "⏳ 待開盤", None
  implied_prob = 1.0 / win_odds
  edge = model_prob - implied_prob
  if edge >= 0.05:
    return "💎 超值冷門", round(edge * 100.0, 1)
  if edge >= 0.015:
    return "✅ 具值博率", round(edge * 100.0, 1)
  if edge >= -0.025:
    return "⚖️ 賠率合理", round(edge * 100.0, 1)
  return "⚠️ 偏熱/低值", round(edge * 100.0, 1)


def rank_overseas_race(race: dict[str, Any]) -> dict[str, Any]:
  runners = list(race.get("runners") or [])
  if not runners:
    return {**race, "ranked_runners": []}

  distance_m = safe_int_dist(race.get("distance"))
  class_score = score_class_tier(race.get("class_name", ""))

  weights = [_to_float(r.get("weight_lbs"), 125.0) or 125.0 for r in runners]
  min_w, max_w = min(weights), max(weights)
  field_size = len(runners)

  has_odds = any((_to_float(r.get("win_odds")) or 0.0) > 1.0 for r in runners)

  enriched: list[dict[str, Any]] = []
  raw_scores: list[float] = []

  for r in runners:
    w_lbs = _to_float(r.get("weight_lbs"), 125.0) or 125.0
    draw = int(_to_float(r.get("draw"), 7) or 7)
    rating = _to_float(r.get("rating"), 80.0) or 80.0
    win_odds = _to_float(r.get("win_odds"), None)
    style = str(r.get("running_style") or "前置機動")

    wd_score = score_weight_and_draw(
        w_lbs, min_w, max_w, draw, field_size, distance_m
    )
    pace_score = score_pace_fit(style, draw, distance_m)
    rating_norm = max(45.0, min(98.0, rating))

    odds_score = 70.0
    if win_odds is not None and win_odds > 1.0:
      implied_p = 1.0 / win_odds
      odds_score = max(40.0, min(98.0, 55.0 + implied_p * 120.0))

    if has_odds:
      osi = (
          0.25 * class_score
          + 0.25 * wd_score
          + 0.20 * pace_score
          + 0.15 * rating_norm
          + 0.15 * odds_score
      )
    else:
      osi = (
          0.32 * class_score
          + 0.30 * wd_score
          + 0.23 * pace_score
          + 0.15 * rating_norm
      )

    osi_rounded = round(osi, 1)
    raw_scores.append(math.exp(osi / 12.0))

    enriched.append({
        **r,
        "overseas_score": osi_rounded,
        "criteria_breakdown": {
            "class_score": class_score,
            "weight_draw_score": wd_score,
            "pace_score": pace_score,
            "rating_score": round(rating_norm, 1),
            "odds_score": round(odds_score, 1) if has_odds else None,
        },
    })

  total_exp = sum(raw_scores) or 1.0
  for item, exp_val in zip(enriched, raw_scores):
    win_prob = exp_val / total_exp
    win_odds = _to_float(item.get("win_odds"), None)
    badge, edge_pct = evaluate_value_bet(win_prob, win_odds)
    item["model_win_prob"] = round(win_prob * 100.0, 1)
    item["value_badge"] = badge
    item["value_edge_pct"] = edge_pct

  enriched.sort(
      key=lambda x: (
          x["overseas_score"],
          -(_to_float(x.get("win_odds"), 999.0) or 999.0),
          -(_to_float(x.get("weight_lbs"), 0.0) or 0.0),
      ),
      reverse=True,
  )

  for idx, item in enumerate(enriched, start=1):
    item["rank"] = idx

  return {
      **race,
      "ranking_mode": "overseas_criteria_v1",
      "ranked_runners": enriched,
  }


def safe_int_dist(dist_raw: Any) -> int:
  val = _to_float(dist_raw, 1600.0)
  return int(val) if val is not None and val > 0 else 1600


def rank_overseas_races(
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
  payload = data
  if payload is None:
    payload = json.loads(INPUT_DATA_PATH.read_text(encoding="utf-8"))

  all_races = list(payload.get("races") or [])
  overseas_races = [
      r for r in all_races if str(r.get("race_type", "")).lower() == "overseas"
  ]

  ranked_list = [rank_overseas_race(r) for r in overseas_races]
  output = {
      "race_date": payload.get("race_date", ""),
      "ranking_agent": "overseas_ranking_agent",
      "total_overseas_races": len(ranked_list),
      "races": ranked_list,
  }

  OUTPUT_RANKED_PATH.parent.mkdir(parents=True, exist_ok=True)
  OUTPUT_RANKED_PATH.write_text(
      json.dumps(output, ensure_ascii=False, indent=2),
      encoding="utf-8",
  )
  return output


if __name__ == "__main__":
  rank_overseas_races()
