# Third-party notices

VoxInk's own source code is released under the [MIT License](LICENSE). The
components below belong to their authors and keep their own licenses.

## Bundled in this repository

### React, React DOM, and Scheduler

`voxink_web/static-dist/` is a prebuilt copy of the web console. Its
JavaScript includes React 19.2.6, React DOM 19.2.6, and Scheduler 0.27.0,
distributed under the following license:

```
MIT License

Copyright (c) Meta Platforms, Inc. and affiliates.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

### Speech sample

`samples/asr_en.wav` is a 14.6-second excerpt (the first sentence) of a
LibriVox volunteer's reading of Abraham Lincoln's Gettysburg Address, file
`gettysburgaddress_lincoln_fb.mp3` from the
[150th-anniversary collection](https://archive.org/details/gettysburgaddress_150anniv_1311_librivox).
The recording is in the public domain
([Public Domain Mark 1.0](https://creativecommons.org/publicdomain/mark/1.0/)),
and it was resampled to 48 kHz 24-bit mono.

## Downloaded separately by users

VoxInk does not include these. Each user downloads them and accepts their
terms directly.

| Model | Source | License |
| --- | --- | --- |
| Qwen3-ASR 1.7B and 0.6B | [Qwen on Hugging Face](https://huggingface.co/Qwen) | Apache-2.0 |
| Qwen3-ForcedAligner-0.6B | [Qwen/Qwen3-ForcedAligner-0.6B](https://huggingface.co/Qwen/Qwen3-ForcedAligner-0.6B) | Apache-2.0 |
| Qwen3-0.6B (translation) | [Qwen/Qwen3-0.6B](https://huggingface.co/Qwen/Qwen3-0.6B) | Apache-2.0 |
| pyannote speaker-diarization-community-1 | [pyannote/speaker-diarization-community-1](https://huggingface.co/pyannote/speaker-diarization-community-1) | CC-BY-4.0, gated: accept its conditions on Hugging Face first |

Speaker diarization uses pyannote's Community-1 pipeline by pyannoteAI and
contributors, licensed under
[CC-BY-4.0](https://creativecommons.org/licenses/by/4.0/).

Python packages (`requirements*.txt`) and JavaScript packages
(`voxink_web/package.json`) are installed by `pip` and `pnpm`/`npm` under
their own licenses and are not included in this repository.
