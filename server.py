from flask import Flask, render_template, make_response
from pydub import AudioSegment
from io import BytesIO
from tomllib import load as toml_load
from pathlib import Path
from mimetypes import guess_type
import mutagen

with Path(__file__).with_name("game.toml").open("rb") as config_file:
  config = toml_load(config_file)

music_root = Path(config["root-directory"])
musics = []
for base, _, files in music_root.walk():
  for file in files:
    full_path = base / file
    mime_type, _ = guess_type(full_path)
    if mime_type is not None and mime_type.startswith("audio/"):
      # save the full path and collect possible name from filename and metadata
      information = {
        "path": full_path,
        "names": [full_path.stem],
      }
      metadata = mutagen.File(full_path)
      if "Title" in metadata:
        information["names"].extend(metadata["Title"])
      musics.append(information)
print(musics)

app = Flask(__name__)

@app.route("/")
def home():
    return render_template("index.html")
@app.route("/music/<uuid:music_id>")
def get_full_music(music_id):
  print(music_id)
  a = AudioSegment.from_file("test.flac")
  b = BytesIO()
  a[:5000].export(b, "flac")
  response = make_response(b.read())
  response.headers["Content-Type"] = "audio/flac"
  return response