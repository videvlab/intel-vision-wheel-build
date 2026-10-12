# Exact upstream Intel Torchvision wheel

This repository builds the original public Torchvision 0.29.1 source at commit `1f1a920d685d46dfbe187934b785b7ecb7a182f2` for CPython 3.13.7 and macOS Intel (deployment target 14.0). It reuses the existing, unmodified Torch 2.14.1 wheel built from original public commit `5c4886908584029761b579af026dcfb627c84070`. Torch and ONNX Runtime are not rebuilt here.

All interpreter and build dependencies are public upstream downloads pinned by SHA256. The workflow uses one standard GitHub-hosted Intel Mac runner. Native import, Torch CPU multiplication and Torchvision NMS are checked. Logs, source identity, wheels and original notices are retained even on failure. A wheel build alone is not acceptance of a complete application.

This repository contains no video product source, presets, model weights, authentication, user files or private CI provenance. Upstream libraries retain their original licenses. The Torch release includes the original wheel, all 146 source notices and a public source/hash summary; the original private build record remains private.
