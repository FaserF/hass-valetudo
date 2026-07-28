import json
import os

BASE_PATH = os.path.dirname(os.path.realpath(__file__))
with open(os.path.join(BASE_PATH, "manifest.json"), encoding="utf-8") as f:
    MANIFEST = json.load(f)

VERSION = MANIFEST["version"]
