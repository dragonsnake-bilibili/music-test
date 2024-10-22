#!/bin/env python

from __future__ import annotations

import tkinter
from abc import abstractmethod
from concurrent.futures import Future, ThreadPoolExecutor
from functools import partial
from mimetypes import guess_type
from pathlib import Path
from random import shuffle
from threading import Event
from tkinter import filedialog, messagebox
from traceback import print_exception
from typing import Self

import mutagen
import pyaudio
from pyaudio import PyAudio
from pydub import AudioSegment


class Form:
  """Base class of forms to be shown on window."""

  def load_to(self, window: Window) -> None:
    """Load this form to window."""
    window.form = self

  def unload(self, window: Window) -> None:
    """Unload this form from window."""
    window.form = None


class StartPage(Form):
  """Start page: picking directory and select difficulty."""

  def __init__(self) -> Self:
    """Init objects to be used in start page."""
    super().__init__()

    # start button
    self._start = tkinter.Button(text="Start!")

    # path selector
    self._selector = tkinter.Button(
      text="Select Directory",
      command=partial(StartPage.select_directory, self),
    )
    self._path_view = tkinter.Label()
    self._path = None

    # difficulty selector
    self._difficulty_variable = tkinter.StringVar(value="0.5")
    self._frame = tkinter.Frame()
    self._radio = [
      tkinter.Radiobutton(
        text=f"{i} seconds",
        variable=self._difficulty_variable,
        value=f"{i}",
        command=partial(StartPage.update_difficulty, self),
      )
      for i in ["0.5", "1", "2", "5"]
    ]
    self._time = 0.5

  @property
  def _path(self) -> str:
    return self._path_

  @_path.setter
  def _path(self, value: str | None) -> str:
    self._path_ = value
    if value:
      self._path_view.config(text=value)
      self._start.pack(in_=window.window, pady=20)
    else:
      self._path_view.config(text="No directory selected")
      self._start.pack_forget()

  def select_directory(self) -> None:
    """Select an directory."""
    directory = filedialog.askdirectory(mustexist=True)
    self._path = directory

  def update_difficulty(self) -> None:
    """Update variable storing current difficulty."""
    self._time = float(self._difficulty_variable.get())

  def load_to(self, window: Window) -> None:
    super().load_to(window)
    self._selector.pack(in_=window.window, pady=20)
    self._path_view.pack(in_=window.window, pady=20)
    self._frame.pack(in_=window.window)
    for radio in self._radio:
      radio.pack(in_=self._frame, anchor="w")

    def start_game() -> None:
      self.unload(window)
      game = Game(Path(self._path), self._time)
      window.load_form(game)

    self._start.config(command=start_game)

  def unload(self, window: Window) -> None:
    super().unload(window)
    self._selector.destroy()
    self._path_view.destroy()
    self._frame.destroy()
    self._start.destroy()


class Game(Form):
  """Main game form."""

  def __init__(self, path: Path, time: float) -> Self:
    """Load data from last stage and init components need."""
    super().__init__()
    self._path = path
    self._time = time

    self._play = tkinter.Button(text="play")
    self._continue = tkinter.Button(text="continue")
    self._next = tkinter.Button(text="next")

    self._current_index = 0
    self._correct = 0

    self._status = tkinter.Label()
    self._input_value = tkinter.StringVar()
    self._input = tkinter.Entry(textvariable=self._input_value)

    self._audio_data = None
    self._playing: Future | None = None
    self._player_stopper: Event | None = None
    self._finalized = False

    self._audio_server = PyAudio()
    self._thread_pool = ThreadPoolExecutor()

    self._window = None  # we need window to terminate

    # load music from the directory specified
    #  this may take some time, but seems that we have to wait
    self._musics = []
    for base, _, files in path.walk():
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
          self._musics.append(information)
    shuffle(self._musics)

  def update_status(self) -> None:
    self._status.config(
      text=f"Progress: {self._current_index + (1 if self._finalized else 0)} / {len(self._musics)}, "
      f"Succeed: {self._correct} / {self._current_index + (1 if self._finalized else 0)}",
    )

  def load_data(self) -> bool:
    if self._current_index == len(self._musics):
      messagebox.showinfo("Finished!", f"recognized {self._correct} out of {self._current_index} songs")
      self.unload(self._window)
      self._window.load_form(StartPage())
      return False
    self._audio_data = AudioSegment.from_file(self._musics[self._current_index]["path"])
    return True

  def play_audio(self, segment: AudioSegment, event: Event) -> None:
    print("called with", segment, event)
    if segment.sample_width == 1:
      audio_format = pyaudio.paInt8
    elif segment.sample_width == 2:
      audio_format = pyaudio.paInt16
    elif segment.sample_width == 4:
      audio_format = pyaudio.paInt32
    try:
      stream = self._audio_server.open(
        format=audio_format,
        channels=segment.channels,
        rate=segment.frame_rate,
        output=True,
      )
      raw_data = segment.raw_data
      for i in range(0, len(raw_data), 1024):
        stream.write(raw_data[i : i + 1024])
        if event.is_set():
          break

      stream.stop_stream()
      stream.close()
    finally:
      self._playing = None
      self._player_stopper = None

  def play(self) -> None:
    if self._playing is not None:
      return
    self._player_stopper = Event()
    self._playing = self._thread_pool.submit(
      self.play_audio,
      self._audio_data[: int(self._time) * 1000],
      self._player_stopper,
    )

  def play_continue(self) -> None:
    if self._playing is not None:
      return
    if not self._finalized:
      self._finalized = True

    self.update_status()
    self._input_value.set(" or ".join(self._musics[self._current_index]["names"]))

    self._player_stopper = Event()
    self._playing = self._thread_pool.submit(
      self.play_audio,
      self._audio_data[: int(self._time) * 1000],
      self._player_stopper,
    )

  def next(self) -> None:
    self._input.delete(0, tkinter.END)
    if self._playing is not None:
      self._player_stopper.set()
    if not self._finalized:
      self._finalized = True
      self._input_value.set(" or ".join(self._musics[self._current_index]["names"]))
    else:
      self._current_index += 1
      self._input_value.set("")
      self._finalized = False
      if not self.load_data():
        return
    self.update_status()

  def change_callback(self, *args) -> None:
    if self._finalized:
      return
    if self._input_value.get() in self._musics[self._current_index]["names"]:
      self._finalized = True
      self._correct += 1
      self.update_status()

  def load_to(self, window: Window) -> None:
    super().load_to(window)
    self._window = window
    self._status.pack(in_=window.window)
    self._play.pack(in_=window.window)
    self._continue.pack(in_=window.window)
    self._input.pack(
      in_=window.window,
      padx=(window.window.winfo_width() * 0.15, window.window.winfo_width() * 0.15),
      fill=tkinter.X,
    )
    self._next.pack(in_=window.window)
    self.update_status()
    self._play.config(command=partial(Game.play, self))
    self._continue.config(command=partial(Game.play_continue, self))
    self._next.config(command=partial(Game.next, self))
    self._input_value.trace("w", partial(Game.change_callback, self))
    self.load_data()

  def unload(self, window: Window) -> None:
    super().unload(window)
    self._status.destroy()
    self._play.destroy()
    self._continue.destroy()
    self._input.destroy()
    self._next.destroy()
    self._audio_server.terminate()


class Window:
  """Window of the game."""

  def __init__(self) -> Self:
    """Init window of the game."""
    self.window = tkinter.Tk()
    self.window.title("music test")
    self.window.bind("<Escape>", partial(Window.shutdown, self))
    self.window.bind("<Control-q>", partial(Window.shutdown, self))
    self.window.bind("<Control-Q>", partial(Window.shutdown, self))
    self.window.protocol("WM_DELETE_WINDOW", partial(Window.shutdown, self))

    self.form = None

  def shutdown(self, _: tkinter.Event | None = None) -> None:
    """Shutdown this game."""
    self.window.destroy()

  def mainloop(self) -> None:
    """Do mainloop."""
    self.window.mainloop()

  def load_form(self, form: Form) -> None:
    """Load form to window."""
    form.load_to(self)


window = Window()
start_page = StartPage()
window.load_form(start_page)
window.mainloop()
