"""Bundled wordlists.

Shipped as package data so ``lexicon.load_wordlist()`` works from an installed
wheel as well as from a source checkout. The canonical 4-letter list lives at
``words4.txt`` in this directory; add a ``.txt`` file here and pass its name to
``load_wordlist()`` to extend or replace it.
"""

from __future__ import annotations
