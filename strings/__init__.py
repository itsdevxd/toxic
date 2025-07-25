import os
from typing import List
import yaml

languages = {}
languages_present = {}

# Load English first for fallback
with open("./strings/langs/en.yml", encoding="utf8") as f:
    languages["en"] = yaml.safe_load(f)
languages_present["en"] = languages["en"].get("name", "English")

for filename in os.listdir("./strings/langs/"):
    if not filename.endswith(".yml"):
        continue
    language_name = filename[:-4]
    if language_name == "en":
        continue
    file_path = os.path.join("./strings/langs/", filename)
    with open(file_path, encoding="utf8") as f:
        languages[language_name] = yaml.safe_load(f)
    # Fill missing keys from English
    for item in languages["en"]:
        if item not in languages[language_name]:
            languages[language_name][item] = languages["en"][item]
    try:
        languages_present[language_name] = languages[language_name]["name"]
    except KeyError:
        print(f"Issue with language file: {filename}")
        exit()

def get_string(lang: str):
    return languages[lang]
    
