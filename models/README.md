# Local models

Place downloaded transcription models in this directory. The default VoxInk
configuration expects Qwen3-ASR at `models/Qwen3-ASR-1.7B/`.

Qwen3-ASR-0.6B is the optional efficient profile. Select it explicitly with:

```sh
./scripts/voxink-dev --model models/Qwen3-ASR-0.6B --device auto
```

Compare it with the default profile using:

```sh
./scripts/voxink-bench --model models/Qwen3-ASR-0.6B --device auto
```

The local browser UI discovers complete immediate-child model packages through
their `voxink-model-manifest.json` files. It can switch installed profiles while
capture is stopped; command-line selection remains the default for each launch.

Model weights are runtime assets and are intentionally excluded from Git.
