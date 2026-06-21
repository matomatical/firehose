"""
Tests for firehose.sample's pure helpers. _key_to_command maps a raw keypress to
a semantic Scanner command; it is a pure lookup over readchar constants and needs
no terminal. (The Scanner side of the input layer — commands -> effects — is
covered in test_scanner.py; this covers keys -> commands.)
"""

import readchar

from firehose.sample import _key_to_command


def test_key_to_command_letters():
    assert _key_to_command("q") == "quit"
    assert _key_to_command("o") == "open"
    assert _key_to_command("s") == "save"
    assert _key_to_command("d") == "download"
    assert _key_to_command("x") == "remove"


def test_key_to_command_special_keys():
    assert _key_to_command(readchar.key.ESC) == "quit"
    assert _key_to_command(readchar.key.LEFT) == "back"
    assert _key_to_command(readchar.key.RIGHT) == "forward"
    assert _key_to_command(readchar.key.SPACE) == "pause"
    assert _key_to_command(readchar.key.UP) == "open"
    assert _key_to_command(readchar.key.DOWN) == "down"


def test_key_to_command_unknown_is_none():
    assert _key_to_command("z") is None
    assert _key_to_command("1") is None
