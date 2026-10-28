# Models

Each AI feature runs on a model that is downloaded once into the data folder. Where a feature has more than one model, the default is the small, fast one; **Maintenance › AI analysis and models** shows the sizes and speeds and switches to a larger, more accurate one.

| Feature | Default model | Alternatives | Download |
| --- | --- | --- | --- |
| Content search | SigLIP 2 base | CLIP ViT-B/32 (faster to index), Jina CLIP v1 (longer, more natural queries) | 0.6 to 1.5 GB |
| Object detection | YOLOX-S | YOLOX-M, -L, -X (more accurate, slower) | 35 to 400 MB |
| Text in photos | RapidOCR | | about 60 MB |
| Face grouping | SFace | ArcFace ResNet-50 (much better at telling people apart, slower) | 37 to 174 MB |
| Face detection | YuNet | SCRFD 10G, RetinaFace ResNet-50 (find more faces, slower) | 0.2 to 104 MB |
| Photo descriptions | BLIP base | BLIP large (richer sentences, slower), Florence-2 base (a short paragraph per photo, no PyTorch needed, several times slower) | about 1 to 2 GB |
| Photo quality | LAION aesthetic predictor on CLIP ViT-L/14 | | about 1.2 GB |
| Closed eyes | a ViT eye-state classifier | | about 340 MB, plus a face detection model |
| Date suggestions | MiVOLO v2 (age) | | about 120 MB |
| Places | GeoNames | | about 30 MB |

## Switching models

- **Content search**: the library is indexed again with the new model in the background. Search keeps using the old index until the new one is complete, and you can switch back at any time.
- **Objects and descriptions**: new photos use the new model at once; **Run again** redoes the library.
- **Face grouping**: every face is processed again in the background.
- **Face detection**: the next detection run uses it.

## Downloading from the command line

The model keys are: `clip`, `siglip2`, `jinaclip`, `blip`, `blip_large`, `yolox`, `yolox_m`, `yolox_l`, `yolox_x`, `yunet`, `scrfd`, `retinaface`, `sface`, `arcface`, `florence2`, `aesthetic`, `eyes`, `mivolo`, `geonames`, `ocr`.

```sh
# checkout or installer
.venv/bin/python -m app.ai.models download siglip2
# Docker
docker compose exec -e HF_HUB_OFFLINE=0 iopix python -m app.ai.models download siglip2
```

`AUTO_MODELS=siglip2,yolox,geonames` downloads missing models at start; the Docker image and the installers use it.

## Speed

Everything runs on the CPU with ONNX, one job at a time. Photo descriptions and the aesthetic score are the slowest; BLIP uses the GPU on a Mac with Apple silicon, Florence-2 runs on the CPU only. A first full pass over a large library can take a day or more; the library is fully usable meanwhile.
