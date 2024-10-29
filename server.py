"""Server for the music recognizing game."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from functools import partial
from io import BytesIO
from mimetypes import guess_type
from pathlib import Path
from random import randint, shuffle
from tomllib import load as toml_load
from typing import Any, Self
from uuid import uuid4

import flask
import mutagen
from flask import Flask, make_response, render_template, request
from pydub import AudioSegment

with Path(__file__).with_name("game.toml").open("rb") as config_file:
  config = toml_load(config_file)

skip_start = config["segment-selection"]["skip-start"]
skip_end = config["segment-selection"]["skip-end"]
play_time = int(config["segment-selection"]["length"] * 1000)

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
        "id": uuid4().hex,
      }
      metadata = mutagen.File(full_path)
      if "Title" in metadata:
        information["names"].extend(metadata["Title"])
      musics.append(information)

candidates = [
  {
    "name": " or ".join(f"[{name}]" for name in music["names"]),
    "id": music["id"],
  }
  for music in musics
]


class MusicCache:
  """Cache holding audio data for musics.

  Each session can preload up to three musics, this global cache holder is provided to allow different
   sessions sharing their cache if they need the same music.
  """

  def __init__(self) -> Self:
    """Create an empty cache."""
    self._audio_data = {}
    self._executor = ThreadPoolExecutor()

  def _load_music(self, path: Path) -> AudioSegment:
    return AudioSegment.from_file(path)

  def _increase_reference(self, music_id: str) -> None:
    target = self._audio_data[music_id]
    target["references"] += 1

  def _decrease_reference(self, music_id: str) -> None:
    target = self._audio_data[music_id]
    target["references"] -= 1
    if target["references"] == 0:
      del self._audio_data[music_id]

  def load(self, music: dict[str, Any]) -> None:
    """Require to load a music."""
    if music["id"] in self._audio_data:
      self._increase_reference(music["id"])
      return
    self._audio_data[music["id"]] = {
      "references": 1,
      "task": self._executor.submit(
        partial(MusicCache._load_music, self),
        music["path"],
      ),
    }

  def get(self, music_id: str) -> AudioSegment | None:
    """Get audio data by id. If such music is not previously loaded, None will be returned."""
    if music_id not in self._audio_data:
      return None
    if "task" in self._audio_data[music_id]:
      # still loading, we must wait till the data is ready
      task = self._audio_data[music_id]["task"]
      audio = task.result()
      del self._audio_data[music_id]["task"]
      self._audio_data[music_id]["data"] = audio
    return self._audio_data[music_id]["data"]

  def unload(self, music_id: str) -> None:
    """Request to unload audio data that is no longer in use.

    Under the hood, the reference counter is reduced. Actual removal will happen only when such counter
     reaches 0.
    """
    if music_id not in self._audio_data:
      return
    self._decrease_reference(music_id)


music_cache = MusicCache()


class Session:
  """Client session."""

  def __init__(self) -> Self:
    """Initialize an client session."""
    # shuffle musics by picking a permutation randomly
    indices = list(range(len(musics)))
    shuffle(indices)
    self._musics = (musics[i] for i in indices)
    # create an identifier for this session
    self._session_id = uuid4().hex
    # current progress
    self._guessed = 0
    self._succeed = 0
    self._finalized = False  # if current music is guessed
    # start time point of current music
    self._start_time = 0
    # current and near-future musics
    self._active_musics = []

    # initialize session cache
    for _ in range(3):
      self._active_next_music()

  def _active_next_music(self) -> dict[str, Any] | None:
    try:
      music = next(self._musics)
      music_cache.load(music)
      self._active_musics.append(music)
    except StopIteration:
      return None

  def guess(self, music_id: str) -> bool:
    """Guess the music and check if the guess was correct."""
    current_music = self._current_music
    if current_music is None:
      return False

    result = current_music["id"] == music_id
    if not self._finalized:
      if result:
        self._succeed += 1
      self._finalized = True

    return result

  def step(self) -> None:
    """Move to next music.

    This will:
     1. unload current music from global music cache
     2. pop current music from active music list
     3. active next music in music list
     4. reset start time to None
     5. update number of guessed musics
     6. reset finalized status
    """
    current_music = self._current_music
    if current_music is None:
      return

    music_cache.unload(current_music["id"])
    self._active_musics.pop(0)
    self._active_next_music()
    self._start_time = None
    self._guessed += 1
    self._finalized = False

  def logout(self) -> None:
    """Cleanup and close this session."""
    # unload all cached musics from global cache
    for music in self._active_musics:
      music_cache.unload(music["id"])

  @property
  def session_id(self) -> str:
    """Get session id."""
    return self._session_id

  @property
  def _current_music(self) -> dict[str, Any] | None:
    if len(self._active_musics) == 0:
      return None
    return self._active_musics[0]

  @property
  def full_music(self) -> bytes | None:
    """Get full-length audio data of current music."""
    music = self._current_music
    if music is None:
      return None
    # requesting full music indicates failure in guessing this music
    if not self._finalized:
      self._finalized = True
    buffer = BytesIO()
    music_cache.get(music["id"]).export(buffer, "flac")
    return buffer.read()

  @property
  def answer(self) -> str | None:
    """Get id of current music."""
    music = self._current_music
    if music is None:
      return None
    # requesting answer indicates failure in guessing this music
    if not self._finalized:
      self._finalized = True
    return music["id"]

  @property
  def sliced_music(self) -> bytes | None:
    """Get test-length audio data of current music."""
    music = self._current_music
    if music is None:
      return None
    buffer = BytesIO()
    audio = music_cache.get(music["id"])
    if self._start_time is None:
      total = len(audio)
      self._start_time = randint(  # noqa: S311
        int(total * skip_start),
        min(
          int(total * (1 - skip_end)),
          total - play_time,
        ),
      )
    audio[self._start_time : self._start_time + play_time].export(buffer, "flac")
    return buffer.read()

  @property
  def finished(self) -> bool:
    """Tell if this session have finished all guessing."""
    return self._guessed == len(musics)

  @property
  def summarize(self) -> dict[str, int | float]:
    """Summarize current session."""
    return {
      "total": len(musics),
      "current_index": self._guessed + 1,
      "guessed": self._guessed + (1 if self._finalized else 0),
      "correct": self._succeed,
    }


app = Flask(__name__)
sessions: dict[str, Session] = {}

@app.get("/")
def home() -> flask.typing.t.Text:
  """Serve HTML webpage."""
  return render_template("index.html")


@app.get("/login")
def login() -> flask.Response:
  """Register a session."""
  response = make_response("", 204)
  session_id = request.cookies.get(key="id", default="", type=str)
  session = sessions.get(session_id)
  if session is None:
    uuid = uuid4().hex
    response.set_cookie("id", uuid, httponly=True, samesite="Strict")
    sessions[uuid] = Session()
  return response


@app.get("/logout")
def logout() -> flask.Response:
  """Logout a session."""
  session_id = request.cookies.get(key="id", default="", type=str)
  session = sessions.get(session_id)
  if session is None:
    return make_response("", 403)
  session.logout()
  del sessions[session_id]
  response = make_response("", 204)
  response.delete_cookie("id")
  return make_response("", 204)


def get_music(session: Session, *, sliced: bool) -> flask.Response:
  """Generate response for requests to music data."""
  audio_data = session.sliced_music if sliced else session.full_music
  if audio_data is None:
    response = make_response("", 404)
  else:
    response = make_response(audio_data)
    response.headers["Content-Type"] = "audio/flac"
  return response


@app.get("/music")
def get_full_music() -> flask.Response:
  """Serve full-length music."""
  session_id = request.cookies.get(key="id", default="", type=str)
  session = sessions.get(session_id)
  if session is None:
    return make_response("", 403)
  return get_music(session, sliced=False)


@app.get("/test-music")
def get_sliced_music() -> flask.Response:
  """Serve full-length music."""
  session_id = request.cookies.get(key="id", default="", type=str)
  session = sessions.get(session_id)
  if session is None:
    return make_response("", 403)
  return get_music(session, sliced=True)


@app.get("/answer")
def get_answer() -> flask.Response:
  """Get answer of current session."""
  session_id = request.cookies.get(key="id", default="", type=str)
  session = sessions.get(session_id)
  if session is None:
    return make_response("", 403)
  answer = session.answer
  if answer is None:
    return make_response("", 404)
  return make_response({"answer": answer})


@app.post("/guess")
def handle_guess() -> flask.Response:
  """Handle guesses from client."""
  session_id = request.cookies.get(key="id", default="", type=str)
  session = sessions.get(session_id)
  if session is None:
    return make_response("", 403)
  guess = request.json["guess_id"]
  result = session.guess(guess)
  return make_response({"result": result})


@app.get("/next")
def move_forward() -> flask.Response:
  """Forward this session to next music."""
  session_id = request.cookies.get(key="id", default="", type=str)
  session = sessions.get(session_id)
  if session is None:
    return make_response("", 403)
  session.step()
  if session.finished:
    response = make_response(session.summarize)
    response.delete_cookie("id")
    return response
  return make_response("", 204)


@app.get("/summarize")
def get_summarize() -> flask.Response:
  """Summarize this session."""
  session_id = request.cookies.get(key="id", default="", type=str)
  session = sessions.get(session_id)
  if session is None:
    return make_response("", 403)
  return make_response(session.summarize)


@app.get("/candidates")
def get_candidates() -> list[dict[str, str]]:
  """Get candidates."""
  return candidates
