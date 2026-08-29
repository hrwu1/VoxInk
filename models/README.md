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

Model weights are runtime assets and are intentionally excluded from Git.
