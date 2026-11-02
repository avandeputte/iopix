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

For photos marked **Date unknown**, **Explore › Date suggestions** (for editors) proposes a year from every clue the library has about them:

- **how old the named people look**: each person's photos with a known date teach the age model how that person ages; photos dated only to the year or month count too, with less weight. A birth year you enter on the page helps someone with few dated photos, and no suggestion is ever earlier than an entered birth year;
- **when the same people are photographed**: the years with a date in which the photo's named people appear, together when they are photographed together often enough, else each on their own;
- **what it looks like**: the dated photos that look most like it, from the content-search model (film grain, colour, clothes and the scene itself place a print or a scan in its years);
- **the camera**: the years the same camera took dated photos.

Each clue gives a year with an uncertainty; the suggestion is their weighted mean, shown as a range with the clues it came from, and the closest ranges come first. **Folder hints** on the page (the earliest and latest year anything in a folder can be from, subfolders included) keep every suggestion in that folder within those years and leave dated photos outside them out of the clues; an age estimate far outside the window is dropped rather than moved to its edge, so a photo whose only clue contradicts the hint gets no suggestion. Photos you date to the year or month (a used suggestion, a year typed in) count as references for the other clues, so dating a few photos of a period by hand improves the suggestions for the rest. A photo without named people still gets a suggestion from how it looks or the camera. **Use** saves it as a year-only date, with undo in the edit log; nothing is written before that. The info panel of an undated photo shows its suggestion and clues too.

## Place suggestions

For photos without a position, **Explore › Place suggestions** (for editors) proposes where they were taken, from the photos around them that have one: a camera without GPS shot among phone photos with it. In order of confidence:

- **between** a photo with a position taken shortly before and one shortly after, close to each other: the position is interpolated by time;
- **near** one photo with a position taken within the hour;
- **that day**: the places photographed that day, grouped within a couple of kilometres. One place takes the photo; among several, the one it looks most like (from the content-search model), else the one nearest in time;
- **a stay**: no position that day at all, but photos at one place both the day before and the day after.

Every suggestion says how far off it may be. **Use** saves it as an approximate position (the accuracy is written with it), with undo in the edit log; nothing is written before that. The info panel of a photo without a position shows its suggestion too, and the location editor still lists the nearest photos with a position.

## Jersey numbers

On each detected person, text recognition reads the number on the shirt. Photos are searchable by it (**Jersey number 10** as a filter chip, `jersey=10` in the address), and the info panel lists the numbers read. Where the person wearing a number has a named face, that player is learnt for the folder and the year. A number on someone whose face is not named, seen from behind or too small for a face, then suggests the player on **Explore › Jersey numbers** (for editors), when one player clearly wears it in the same folder (else the same year). **Tag** adds the person to the photo without a face region, with undo in the edit log; nothing is tagged before that.

## Clothing matches

At an event everyone wears the same clothes all day. The content-search model turns each detected person into an embedding that mostly describes what they wear. Within the photos of one folder on one day, a person whose face is not named (from behind, too far, too small for a face) is compared with the people whose face is named; when the closest match is clear, **Explore › Clothing matches** (for editors) suggests that person is in the photo too, and the info panel shows it under *Probably here, by the clothes*. **Tag** adds the person to the photo without a face region, with undo in the edit log; nothing is tagged before that. The embeddings are computed again when you switch the search model.

## Wrongly rotated

Scans and old files are often stored on their side or upside down. No extra model is needed: your own photos teach a small classifier on the content-search embeddings what upright looks like (a sample is embedded as it is and turned three ways). Every photo whose embedding reads as turned is then checked in all four turns, and only the ones the four readings agree on are listed under **Maintenance › Wrongly rotated**, each shown the way it would look after the turn. **Rotate** is the ordinary lossless rotate (the file is turned, the metadata kept, digiKam's row updated, undo in the edit log); **It's fine** leaves a photo out until its file changes. The info panel shows the finding too. The classifier is learnt again when you switch the search model.

## Smart crop

A square or wide tile crops the photo to its own shape, and a centre crop cuts heads off portraits. Each photo has a focus point, the centre of its subject: the faces digiKam knows, else the people object detection found, else the middle of the frame. The Explore tiles, the covers of folders, albums, people and places, and the apps' tiles keep that point in view, and `/thumb/<id>?fit=square` (or `fit=16x9`) serves a thumbnail cropped around it. Nothing is written to any photo; the point lives in the search index and moves with the faces.

## Videos

For each video, iopix picks the **cover frame** from several frames spread over it (sharp, well lit, with a face when the face detector is installed) instead of a frame a quarter of the way in; the thumbnail and preview are made from it. **Chapter thumbnails** under the player mark the scene changes (or, in a single take, moments spread evenly): click one to jump there. With a speech model installed (**Maintenance › AI analysis and models**: Parakeet by default, Whisper base as the small alternative), **what is said** is transcribed on the CPU, searchable from the search box, and shown in the info panel with the moments it was said, to jump to. Nothing is written to any video.

## Text in photos

Text that recognition finds in a photo (signs, jerseys, documents) is searchable and shown in the info panel. Only what reads as words is kept: numbers, words from English, French, Dutch and German word lists, the names of your people, tags, folders and places, your titles and captions, and words read in three or more of your photos (a team's or a school's name). Misreadings such as "NIVHITY" on a curved sweatshirt are left out.

## In the info panel

Everything the models produced is gathered at the end of the info panel under **AI analysis**: the description, detected content, objects, text found in the picture, photo quality (focus, aesthetic score, closed eyes, and why it may be a reject) and similar photos.
