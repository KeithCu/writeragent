# WriterAgent - vendored from Pooch v1.8.2 pooch/hashes.py (BSD-3-Clause).
from __future__ import annotations

import functools
import hashlib
from pathlib import Path

ALGORITHMS_AVAILABLE = {
    alg: getattr(hashlib, alg, functools.partial(hashlib.new, alg))
    for alg in hashlib.algorithms_available
}


def file_hash(fname: str, alg: str = "sha256") -> str:
    if alg not in ALGORITHMS_AVAILABLE:
        raise ValueError(f"Algorithm {alg!r} not available")
    chunksize = 65536
    hasher = ALGORITHMS_AVAILABLE[alg]()
    with open(fname, "rb") as fin:
        buff = fin.read(chunksize)
        while buff:
            hasher.update(buff)
            buff = fin.read(chunksize)
    return hasher.hexdigest()


def hash_algorithm(hash_string: str | None) -> str:
    default = "sha256"
    if hash_string is None:
        return default
    if ":" not in hash_string:
        return default
    return hash_string.split(":")[0].lower()


def hash_matches(fname: str | Path, known_hash: str | None, *, strict: bool = False, source: str | None = None) -> bool:
    if known_hash is None:
        return True
    algorithm = hash_algorithm(known_hash)
    new_hash = file_hash(str(fname), alg=algorithm)
    matches = new_hash.lower() == known_hash.split(":")[-1].lower()
    if strict and not matches:
        label = source if source is not None else str(fname)
        raise ValueError(
            f"{algorithm.upper()} hash of downloaded file ({label}) does not match "
            f"the known hash: expected {known_hash} but got {new_hash}."
        )
    return matches
