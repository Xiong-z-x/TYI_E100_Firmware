# Vision gateway third-party notices

## Ultralytics

The deployment base image is:

```text
ultralytics/ultralytics:latest-jetson-jetpack5
manifest digest:
sha256:9883d4b8ff116473de860e22baaa2b24471228f1107385ddfa45ccf2c0054f36
architecture: linux/arm64
```

The verified manifest is loaded on the Nano under the immutable local tag
`tyi/ultralytics:jetpack5-20260724`; repository builds use that local tag and
therefore do not depend on a future change to Docker Hub's `latest` tag.

The image declares `AGPL-3.0-or-later` and links its source to
<https://github.com/ultralytics/ultralytics>. The runtime reuses this image's
JetPack 5 CUDA, TensorRT, PyTorch and OpenCV environment; it does not load an
Ultralytics YOLO checkpoint.

Any redistribution or non-competition product use must be reviewed against the
applicable Ultralytics software and model licensing terms.

## Google SpeciesNet v4.0.3a

The crop classifier, labels and reference implementation are published by
Google's Camera Traps AI project:

<https://github.com/google/cameratrapai>

The repository is Apache-2.0 licensed. The exact v4.0.3a weight artifact is
downloaded through the project's official Kaggle link. Model redistribution or
product use must also be checked against the terms attached to that artifact:

<https://www.kaggle.com/models/google/speciesnet>

## MegaDetector v5a

MegaDetector locates animals, people and vehicles before SpeciesNet classifies
each animal crop. The project, documentation and model download information are:

<https://github.com/agentmorris/MegaDetector>

<https://microsoft.github.io/MegaDetector/>

The project documents MegaDetector as free and open-source under the MIT
License. Preserve its attribution and re-check the exact weight/model terms
before redistribution.

## 2025 NUEDC H-problem reference images

The animal posture and terrain images are downloaded from the public NUEDC
problem archive mirrored at:

<https://github.com/chenshuo/nuedc/tree/master/docs/problems>

These binary images are runtime reference/test artifacts under
`models/vision/` and are intentionally excluded from Git.
