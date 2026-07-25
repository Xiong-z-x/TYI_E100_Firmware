# Vision gateway third-party notices

## Ultralytics

The deployment base image is:

```text
ultralytics/ultralytics:latest-jetson-jetpack5
manifest digest:
sha256:9883d4b8ff116473de860e22baaa2b24471228f1107385ddfa45ccf2c0054f36
architecture: linux/arm64
```

The image declares `AGPL-3.0-or-later` and links its source to
<https://github.com/ultralytics/ultralytics>. The YOLOE checkpoint is obtained
from the Ultralytics assets release:

<https://github.com/ultralytics/assets/releases/download/v8.4.0/yoloe-26s-seg.pt>

Any redistribution or non-competition product use must be reviewed against the
applicable Ultralytics software and model licensing terms.

## 2025 NUEDC H-problem reference images

The animal posture and terrain images are downloaded from the public NUEDC
problem archive mirrored at:

<https://github.com/chenshuo/nuedc/tree/master/docs/problems>

These binary images are runtime reference/test artifacts under
`models/vision/` and are intentionally excluded from Git.
