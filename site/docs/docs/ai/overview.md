# AI analysis

iopix can look at your photos and learn what is in them. **All of it runs on your own machine**, on the CPU, and the results are stored in iopix's own database. Nothing is written to digiKam, and no photo is sent to an outside service.

## What it adds

- **Content search**: find photos by describing them, and "more photos like this".
- **Objects**: people, animals, vehicles and everyday things, with boxes.
- **Text in photos**: signs, jerseys, documents, made searchable.
- **Photo descriptions**: one sentence per photo, searchable.
- **Places**: town, region and country from GPS positions.
- **Faces**: look-alike groups, "looks like" suggestions, and finding unmarked faces.
- **Quality**: sharpness and exposure, to find the best shots and the blurry ones.

Each is optional. Choose them during install, or later from **Maintenance › AI analysis and models**.

## What it does not do

AI results are guesses, and iopix treats them that way:

- a suggested name is never written anywhere until you accept it,
- detected content is a filter, not stored tags,
- descriptions are shown with the AI analysis at the end of the info panel, never in place of your own caption.

## Background jobs

New and changed photos are picked up automatically. Heavy work goes through **one queue, one job at a time**, in this order: thumbnails, sharpness and objects, content search, text and face quality, face grouping, similar photos (on request), descriptions.

Each job may use half the machine's cores by default (**Settings › AI and search**, or `BG_THREADS`), so iopix and everything else on the server stay responsive.

**Maintenance › AI analysis and models** shows every job with its progress, **Check for new photos now** and **Run again from scratch**.

## Turning it off

| Variable | Effect |
| --- | --- |
| `ANALYSIS=0` | no background analysis at all |
| `SEMANTIC=0` | no content search, even when installed |
| `CAPTIONS=0` | no photo descriptions |
