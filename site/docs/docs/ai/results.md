# What AI analysis gives you

## Detected content

**Explore › Detected content** files photos under several hundred everyday subjects in groups: landscapes, buildings, rooms, people, events, sports, animals, food, vehicles and travel, objects, documents and photo styles. These come from the content-search model and are filters, not stored labels.

## Bursts and best shots

Photos taken within two seconds on the same camera that also look alike are grouped as a burst. The best frame is the one in focus, with the sharpest faces and nobody's eyes closed, with a small say for the aesthetic score. **Show only the best frame of each burst** is a filter.

## Events and trips

- **Trips** are runs of days photographed far from the place most of your photos are taken.
- **Events** are other days with many photos, named after the title most of them share, else their main tag, else the town.

## Duplicates and similar photos

**Explore › Duplicate files** lists:

- **byte-identical files**, from digiKam's own fingerprints,
- **similar photos** on request: resized, re-saved or burst-neighbour copies, at a strictness you choose, sharpest copy first.

iopix never deletes anything here by itself.

## Quality filters

Sharpness and exposure are measured for every photo: **Sharpest**, **Blurry**, **Very dark** and **Very bright** filters, a **Sharpest first** sort, and sharpness shown as a rank within your own library.

The **Photo quality** job adds:

- **focus** measured on the sharpest part of the frame, so a portrait with a soft background still counts as sharp,
- an **aesthetic score** from about 1 to 10 for light, colour, composition and clarity (it rates the picture, not the moment: a dull snapshot of a moment you love can score low), with a **Best looking first** sort and a **Best looking tenth** filter,
- **closed eyes** on faces seen from the front, with a **Someone's eyes closed** filter.

**Filters › Quality › Likely rejects** lists photos that are blurred all over, have most eyes closed, are clearly worse frames of a burst, or are nearly black or blown out, each with its reason. It is only a suggestion: no label is set on any photo.

## Date suggestions

For photos marked **Date unknown**, **Explore › Date suggestions** (for editors) proposes a year from how old the named people in them look. Each person's photos with a known date teach the age model how that person ages; photos dated only to the year or month count too, with less weight. A birth year you enter on the page helps someone with few dated photos, and no suggestion is ever earlier than an entered birth year. Every suggestion is a range with the people and ages behind it, and **Use** saves it as a year-only date, with undo in the edit log. The info panel of an undated photo shows its suggestion too.

## Text in photos

Text that recognition finds in a photo (signs, jerseys, documents) is searchable and shown in the info panel. Only what reads as words is kept: numbers, words from English, French, Dutch and German word lists, the names of your people, tags, folders and places, your titles and captions, and words read in three or more of your photos (a team's or a school's name). Misreadings such as "NIVHITY" on a curved sweatshirt are left out.

## In the info panel

Everything the models produced is gathered at the end of the info panel under **AI analysis**: the description, detected content, objects, text found in the picture, photo quality (focus, aesthetic score, closed eyes, and why it may be a reject) and similar photos.
