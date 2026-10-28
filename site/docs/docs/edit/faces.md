# Naming faces

Faces in iopix are digiKam's face regions: the same boxes, the same names, written the same way. A name you give in iopix appears in digiKam, and the other way round.

## In the viewer

Press ++f++ to show face boxes. Click a face for its actions:

- **name** it (the person picker can create a new person),
- **confirm** a suggestion,
- **ignore** a face that is not worth naming,
- **forget the name**,
- **adjust the box**,
- **remove** the face.

**Add a face** lets you draw a box where nothing was found. ++p++ jumps to the next unnamed face, or starts drawing one.

## Working through unnamed faces

**People › Faces** collects every face still to name. With the face-grouping model installed:

- look-alike faces are shown together, so a whole group can be named or ignored at once,
- each group carries a **"looks like"** suggestion based on the confirmed faces of every named person, with one-click naming.

Suggestions are only suggestions until you click them.

## Finding new faces

Face detection finds faces nothing has marked yet, on request: one photo, one folder (with its subfolders), or the whole library from Maintenance. Every existing face is respected: a detection that overlaps one is treated as the same face and not added.

## Tidying faces

**Maintenance › Metadata quality** has face checks:

- the same person stored twice a few pixels apart (a leftover of older digiKam versions), with **Remove duplicates**,
- the same person in two separate places on one photo, for review,
- photos where people are in the picture but no face is marked,
- photos with faces where none is named,
- boxes that stick out of the photo, which can be trimmed.

## Where face changes go

Face changes go to digiKam's database and to the photo's sidecar regions, exactly as digiKam writes them. Every face change is in the edit log and can be undone.
