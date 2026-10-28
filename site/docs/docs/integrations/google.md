# Google Maps and Cloud Vision

iopix uses **OpenStreetMap** by default, with nothing to set up. Google is optional.

## Google Maps

1. In the Google Cloud console, create an API key with the **Map Tiles API** and the **Geocoding API** enabled, and billing on.
2. Restrict the key by **HTTP referrer** to your iopix address, because it reaches every browser.
3. Paste it in **Settings › Display › Map provider**, or set `GOOGLE_MAPS_API_KEY`.

The map then uses Google's tiles and satellite imagery, place search uses Google's geocoder, and "open in" links go to Google Maps. The key is stored in iopix's settings and never shown back. If Google refuses, iopix says so and falls back to OpenStreetMap.

## A second key for the server

The searches iopix makes from the server (Geocoding, Places, Elevation, Cloud Vision) can use a separate key restricted by API and IP address. Enter it as the **server key** in the same setting, or `GOOGLE_SERVER_KEY`. Without one, the first key is used for both.

## Street View

Street View opens in Google Maps in a new tab, or is embedded when the key has the **Maps Embed API** enabled.

## Landmarks with Cloud Vision

With the **Cloud Vision API** enabled on a key Google accepts from a server, **Suggest location** can ask Google to recognise a landmark in a photo. Only a 1024-pixel preview of that one photo is sent, and only when you ask.

## Without Google

- Place search uses OpenStreetMap's Nominatim through the server (only the typed text is sent, and results are cached), and the offline town list when there is no connection.
- Satellite imagery comes from Esri World Imagery.
- Altitude comes from Open-Meteo's terrain model.
