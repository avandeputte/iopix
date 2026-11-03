# ioContacts

ioContacts is the address book of the ioThing suite. Linked to it, iopix knows the birthdays of the people named in your library. This is optional and off until an admin sets it up.

## What it adds

- **Birthdays on the People page.** The birthdays in the next month are listed at the top, today's first, with the age each person turns. Click one to see their photos.
- **Ages in the photo info.** Under **People**, each person with a known birth year shows how old they were in that photo. When the photo's date is only known to the year (or to the month of their birthday), it says both possible ages, such as "33 or 34".
- **Better date suggestions.** **Date suggestions** use the birth years from ioContacts, as if you had entered them. A birth year you enter in iopix still wins.

Birthdays are shown to signed-in accounts only (or to everyone while iopix has no accounts). iopix never writes to ioContacts.

## Which contact belongs to which person

iopix uses the links you make in ioContacts: a contact's **ioPix person** (in ioContacts, **Link to ioPix** on a contact, or **Link by name** on its ioPix people page). iopix keeps no mapping of its own. Only linked contacts with a birthday count, so to add someone or fix a match, link the contact in ioContacts.

## Setting it up

In **Settings › ioContacts**, an admin enters:

| Field | What to enter |
|---|---|
| Address | ioContacts' address, such as `https://contacts.example.com`, or its address on your home network |
| User and app password | an ioContacts account that can read the address books, and an app password made for it in ioContacts under **Settings › Apps and devices** |
| Cloudflare client ID and secret | only when ioContacts sits behind Cloudflare Access: a service token (Zero Trust › Access › Service auth) allowed in the ioContacts application's policy |

**Save and test** reads the contacts at once and says how many people it found birthdays for. Clear the address (or **Turn off**) to stop. The same settings can come from `IOCONTACTS_URL`, `IOCONTACTS_USER`, `IOCONTACTS_PASSWORD`, `IOCONTACTS_CF_CLIENT_ID` and `IOCONTACTS_CF_CLIENT_SECRET` in `iopix.env`, which win over the page.

iopix's server reads ioContacts, never your browser, and does so again every ten minutes. When ioContacts can't be reached, pages carry on with the birthdays from the last good read (none just after iopix restarts), and the settings tab shows what went wrong.
