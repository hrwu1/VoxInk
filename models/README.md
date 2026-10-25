# Local models

Each subdirectory holds one model package. Only its
`voxink-model-manifest.json` is tracked; the weights are downloaded into it with:

```sh
./scripts/voxink-models list
./scripts/voxink-models download [MODEL ...]
```

The downloader fetches from the publisher's Hugging Face repository at the
manifest's `revision`, shows the manifest's `license`, and verifies every file
against the manifest's digests. A manifest added for a new model needs
`source_model_id`, `revision`, and `license`; `"gated": true` marks a model whose
conditions the user must accept on Hugging Face first. The default VoxInk
configuration expects Qwen3-ASR at `models/Qwen3-ASR-1.7B/`.

Qwen3-ASR-0.6B is the optional efficient profile. Select it explicitly with:

```sh
./scripts/voxink-app --model models/Qwen3-ASR-0.6B --device auto
```

Compare it with the default profile using:

```sh
./scripts/voxink-bench --model models/Qwen3-ASR-0.6B --device auto
```

The local browser UI discovers complete immediate-child model packages through
their `voxink-model-manifest.json` files. It can switch installed profiles while
capture is stopped; command-line selection remains the default for each launch.

Speaker diarization needs `models/pyannote-speaker-diarization-community-1/`,
and splitting a transcript block between speakers additionally needs
`models/Qwen3-ForcedAligner-0.6B/` (`Qwen/Qwen3-ForcedAligner-0.6B` on the
Hugging Face hub, Apache-2.0, not gated). Both are optional: without the
diarization package speakers are not found; without the aligner, blocks are
labeled whole. Each directory keeps a tracked `voxink-model-manifest.json`
recording the revision and per-file digests the code was built against.

Model weights are third-party runtime assets. They are never committed or
redistributed by VoxInk; users download them from their publishers.
