import json
data = json.loads('{"action":"mute_tonight","hours":Infinity}')
h = float(data["hours"])
print("parsed hours:", h)
print("max(1.0, hours*3600) =", max(1.0, h * 3600.0))
import sys
sys.path.insert(0, ".")
from core.group_memory import GroupMemoryNotebook
from pathlib import Path
import tempfile
d = Path(tempfile.mkdtemp())
n = GroupMemoryNotebook(d)
until = n.mute_tonight("group:x:1", hours=h)
print("mute_until:", until, "-> persisted mute_until:", n._load("group:x:1")["mute_until"])
print("cache keys:", list(n._cache))
n.list_all("random-umo-" + "a"*200)
print("cache keys after arbitrary umo:", len(n._cache))
