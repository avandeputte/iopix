# The edit log and undo

Every change iopix makes to a photo is kept for good: **when**, **who**, **what**, and the values **before and after**.

![The edit log](/img/edit-log.jpg){.shot}

## What is in it

Edits, face changes, tag renames and deletions, syncs, writes into digiKam's database, imports, moves, deletions and restores.

- The **info panel** shows a photo's recent changes, in the section "Change log".
- **Maintenance › iopix › Edit log** shows the whole library's.

## Bringing things back

Each entry offers what can be brought back:

| Action | Does |
| --- | --- |
| **Undo** | applies the values from before, as a new change |
| **Restore sidecar** | puts back the sidecar as it was just before that change |
| **Restore original file** | puts back the image file as it was before iopix first wrote into it |
| **Restore photo** | brings a deleted photo back into the library, with its rating, title, caption and tags |

A restore needs its backup to still be in the data folder, and is itself logged.

## Where the backups are

Sidecar and file backups are never deleted automatically. See [Backups and the data folder](../admin/backups.md) for where they live and how much room to allow.
