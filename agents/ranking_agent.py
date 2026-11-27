import json
from pathlib import Path

DATA_PATH = Path("data/upcoming_races.json")
CONFIG_PATH = Path("config/profiles.json")
OUT_JSON = Path("output/predictions.json")
OUT_MD = Path("output/report.md")


def score_horse(horse: dict, weights: dict) -> float:
  rating = float(horse.get("rating", 60))
  carried_wt = float(horse.get("weight_lbs", 125))
  draw = int(horse.get("draw", 7))
  odds = float(horse.get("win_odds", 10.0))

  # Implied win probability from win odds (1 / odds)
  implied_prob = (1.0 / odds) if odds > 0 else 0.0
  # Penalty for outside draws beyond 5
  draw_diff = max(0, draw - 5)

  score = (
      rating * weights["rating"]
      + (carried_wt - 115) * weights["weight_carried"]
      + draw_diff * weights["draw_penalty"]
      + implied_prob * weights["win_odds_implied_prob"]
  )
  return round(score, 2)


def run_analysis():
  if not DATA_PATH.exists():
    print("No race data found.")
    return

  races = json.loads(DATA_PATH.read_text(encoding="utf-8"))
  profiles = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

  all_results = {}
  md_lines = [f"# HKJC 賽馬優先級分析報告 ({races.get('race_date', 'N/A')})\n"]

  for race in races.get("races", []):
    race_no = race["race_no"]
    md_lines.append(
        f"## 第 {race_no} 場 ({race.get('distance', '')}m - {race.get('course', '')})"
    )

    race_output = {}
    for profile_name, cfg in profiles.items():
      weights = cfg["weights"]
      ranked = []
      for h in race.get("runners", []):
        s = score_horse(h, weights)
        ranked.append({
            "no": h["no"],
            "name": h["name"],
            "draw": h["draw"],
            "weight_lbs": h["weight_lbs"],
            "win_odds": h["win_odds"],
            "score": s,
        })
      ranked.sort(key=lambda x: x["score"], reverse=True)
      for idx, item in enumerate(ranked):
        item["priority"] = (
            "P1 (核心)" if idx < 3 else ("P2 (次選)" if idx < 5 else "P3 (觀察)")
        )
      race_output[profile_name] = ranked

      md_lines.append(f"\n**策略：{profile_name}** — {cfg['description']}")
      for top in ranked[:4]:
        md_lines.append(
            f"- `{top['priority']}` **{top['no']}號 {top['name']}** | 分數:"
            f" {top['score']} | 檔位: {top['draw']} | 負磅: {top['weight_lbs']} |"
            f" 賠率: {top['win_odds']}"
        )
    all_results[f"race_{race_no}"] = race_output
    md_lines.append("\n---\n")

  OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
  OUT_JSON.write_text(
      json.dumps(all_results, ensure_ascii=False, indent=2), encoding="utf-8"
  )
  OUT_MD.write_text("\n".join(md_lines), encoding="utf-8")
  print("Analysis complete.")


if __name__ == "__main__":
  run_analysis()
