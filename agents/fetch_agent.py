def extract_race_meta(body_text: str, default_course: str, race_no: int = 1) -> dict[str, str]:
  full_text = re.sub(r"\s+", " ", body_text or "")
  # Anchor right at "第 X 場" so top navigation menus ("二級賽" etc.) are skipped
  anchor = re.search(rf"第\s*{race_no}\s*場", full_text)
  snippet = full_text[anchor.start() : anchor.start() + 900] if anchor else full_text[:2500]

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
