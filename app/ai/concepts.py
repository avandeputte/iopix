"""A browsable view of what the content-search model sees in the library.

The image-text model stores no labels, only one embedding per photo. To make
that browsable, every photo is compared with a fixed vocabulary of everyday
subjects ("a photo of a beach", ...) and gets the few it matches best. These
are the model's guesses, computed on the fly from the active engine's
embeddings file, and nothing here is written to digiKam.

Labels stay at the level of subject, scene or event ("soccer game", "beach",
"birthday cake"): zero-shot matching cannot tell actions within a sport or
near-synonyms apart, and a too-specific label absorbs everything nearby.
"""
import hashlib
import threading
import time

from app import config
from app.ai.semantic import semantic, enabled, _unit

try:
    import numpy as np
except ImportError:  # only reached when content search is off, and then nothing here runs
    np = None

VOCAB = {
    "Landscapes & nature": [
        "beach", "ocean waves", "tropical beach with palm trees", "rocky coastline", "sand dunes", "lake", "river",
        "stream in the woods", "waterfall", "mountains", "snowy mountain peak", "canyon", "cliff", "cave", "desert",
        "forest", "autumn leaves", "meadow", "wheat field", "vineyard", "rolling hills", "island", "glacier",
        "geyser", "hot spring", "volcano", "swamp", "sunset", "sunrise", "night sky with stars", "full moon",
        "rainbow", "storm clouds", "fog", "snow covered landscape", "frozen lake", "cherry blossoms",
        "tulip field", "sunflower field", "rose garden", "botanical garden", "tree in bloom", "pine trees",
        "palm trees", "cactus", "mushrooms", "moss and ferns"],
    "Cities & buildings": [
        "city skyline", "skyscrapers", "city street", "narrow old town street", "town square", "street market",
        "bridge", "suspension bridge", "castle", "palace", "cathedral", "church interior", "stained glass window",
        "temple", "pagoda", "mosque", "ruins", "monument", "statue", "fountain", "lighthouse", "windmill",
        "tower", "clock tower", "stadium", "arena", "airport terminal", "train station", "subway", "harbor",
        "marina", "canal", "pier", "boardwalk", "cemetery", "university campus", "school building", "library",
        "museum gallery", "art exhibit", "aquarium tank", "zoo enclosure", "amusement park", "roller coaster",
        "ferris wheel", "carousel", "water park", "casino", "neon signs at night", "hotel room", "hotel lobby",
        "shopping mall", "grocery store", "farmers market", "gas station", "parking lot", "highway",
        "construction site", "factory", "barn", "farm", "log cabin", "suburban house", "front porch",
        "backyard", "driveway", "swimming pool", "hot tub", "playground", "treehouse", "campsite", "tent",
        "campfire"],
    "Rooms & indoors": [
        "kitchen", "dining room table", "living room", "bedroom", "bathroom", "nursery", "basement", "garage",
        "home office", "classroom", "school cafeteria", "school hallway", "gymnasium", "locker room",
        "office meeting", "conference room", "hospital room", "doctor's office", "dentist", "hair salon",
        "restaurant interior", "bar", "cafe", "bakery", "movie theater", "theater stage", "bowling alley",
        "arcade", "ice rink", "indoor swimming pool", "fitness gym", "church service", "wedding reception hall",
        "airplane cabin", "inside a car", "train carriage", "elevator", "staircase"],
    "People & portraits": [
        "portrait", "close-up of a face", "selfie", "group selfie", "group photo", "large group photo",
        "family portrait", "couple", "newborn baby", "baby", "toddler", "child", "teenager", "elderly person",
        "grandparents with grandchildren", "mother and child", "father and child", "siblings",
        "friends posing together", "school class photo", "team photo", "school portrait", "passport photo",
        "people hugging", "people kissing", "people laughing", "people dancing", "people singing",
        "people eating together", "people toasting with drinks", "person sleeping", "person crying",
        "person reading", "person cooking", "person working at a computer", "person talking on the phone",
        "child drawing", "child playing with toys", "children playing outside", "kids in a swimming pool",
        "child on a swing", "child on a slide", "baby in a stroller", "baby in a high chair", "pregnant woman",
        "person in a suit", "person in a dress", "person wearing a hat", "person wearing sunglasses",
        "person in a winter coat", "person in a swimsuit", "person wearing a mask", "silly faces",
        "crowd of people", "audience"],
    "Events & celebrations": [
        "birthday party", "birthday cake with candles", "blowing out candles", "opening presents",
        "balloons", "pinata", "wedding ceremony", "bride and groom", "wedding cake", "wedding dance",
        "bridesmaids", "baby shower", "baptism", "first communion", "graduation ceremony",
        "graduate in cap and gown", "diploma", "prom", "homecoming dance", "school dance", "corsage",
        "christmas tree", "christmas presents", "christmas lights", "santa claus", "gingerbread house",
        "nativity scene", "halloween costume", "trick or treating", "jack-o-lantern", "pumpkin patch",
        "easter egg hunt", "easter basket", "thanksgiving dinner", "roast turkey", "fourth of july",
        "fireworks", "new year's eve party", "chinese new year", "lanterns", "dragon dance",
        "valentine's day", "mother's day", "parade", "marching band", "street festival", "carnival",
        "county fair", "concert", "rock concert", "orchestra", "choir", "school play", "school recital",
        "piano recital", "dance recital", "ballet performance", "talent show", "award ceremony", "banquet", "picnic", "barbecue", "pool party",
        "block party", "family reunion", "funeral", "science fair", "spelling bee", "scout meeting",
        "fundraiser", "volunteer work", "building a house", "protest", "press conference"],
    "Soccer": [
        "soccer game", "soccer goalkeeper",
        "soccer team photo",
        "soccer game at night under lights",
        "indoor soccer", "scoreboard"],
    "Other sports": [
        "basketball game",
        "basketball team photo", "baseball game", "softball",
        "american football game", "cheerleaders", "tennis match", "badminton",
        "table tennis", "volleyball", "beach volleyball", "swimming race", "diving board", "water polo",
        "track and field", "running race", "marathon", "cross country race", "high jump", "hurdles",
        "gymnastics", "wrestling", "martial arts", "karate belt ceremony", "boxing", "fencing",
        "ice hockey", "figure skating", "ice skating", "skiing", "snowboarding", "sledding",
        "cross-country skiing", "golf", "mini golf", "bowling", "archery", "horseback riding",
        "cycling race", "mountain biking", "skateboarding", "roller skating", "surfing", "paddleboarding",
        "kayaking", "canoeing", "rafting", "sailing", "rowing", "water skiing", "snorkeling", "scuba diving",
        "fishing", "rock climbing", "hiking", "backpacking", "trail running", "yoga", "weightlifting",
        "dance class", "lacrosse", "field hockey", "rugby", "cricket", "tug of war", "sack race", "sports medal", "sports trophy",],
    "Animals": [
        "dog", "puppy", "dog playing fetch", "cat", "kitten", "horse", "pony", "cow", "pig", "sheep", "goat",
        "chicken", "duck", "goose", "swan", "turkey", "rabbit", "hamster", "guinea pig", "pet fish",
        "parrot", "songbird", "eagle", "owl", "seagull", "pelican", "flamingo", "penguin", "peacock",
        "deer", "elk", "moose", "bison", "bear", "wolf", "fox", "squirrel", "chipmunk", "raccoon",
        "elephant", "giraffe", "zebra", "lion", "tiger", "leopard", "monkey", "gorilla", "panda", "koala",
        "kangaroo", "hippo", "rhino", "camel", "llama", "seal", "sea lion", "dolphin", "whale", "shark",
        "sea turtle", "tortoise", "frog", "lizard", "snake", "alligator", "tropical fish", "jellyfish",
        "starfish", "crab", "butterfly", "bee", "ladybug", "dragonfly", "spider", "caterpillar", "snail"],
    "Food & drink": [
        "pizza", "burger and fries", "hot dog", "sandwich", "tacos", "burrito", "nachos", "pasta", "lasagna",
        "steak", "barbecue ribs", "roast chicken", "fried chicken", "seafood platter", "lobster", "shrimp",
        "oysters", "sushi", "ramen", "dumplings", "hot pot", "stir fry", "fried rice", "curry", "soup",
        "salad", "cheese plate", "charcuterie board", "bread", "bagel", "croissant", "pancakes", "waffles",
        "eggs and bacon", "cereal", "oatmeal", "french fries", "mussels and fries", "belgian waffle",
        "chocolate", "cookies", "cupcakes", "layer cake", "pie", "donuts", "ice cream cone", "sundae",
        "popsicle", "candy", "cotton candy", "popcorn", "fruit bowl", "strawberries", "watermelon", "apples",
        "bananas", "vegetables", "corn on the cob", "pumpkins", "coffee", "latte art", "tea", "bubble tea",
        "milkshake", "smoothie", "juice", "soda", "beer", "glass of wine", "champagne", "cocktail",
        "whiskey", "restaurant menu", "buffet", "dinner table set for a meal", "baking in the kitchen",
        "grilling outdoors", "lunch box", "baby food"],
    "Vehicles & travel": [
        "car", "sports car", "vintage car", "pickup truck", "minivan", "suv", "police car", "fire truck",
        "ambulance", "school bus", "city bus", "tour bus", "taxi", "motorcycle", "scooter", "bicycle",
        "tricycle", "stroller", "golf cart", "tractor", "bulldozer", "excavator", "train", "steam locomotive",
        "tram", "cable car", "ski lift", "airplane", "view from an airplane window", "helicopter",
        "hot air balloon", "sailboat", "motorboat", "cruise ship", "ferry", "canoe", "fishing boat",
        "rowboat", "jet ski", "rv camper", "road trip", "luggage", "passport", "boarding pass",
        "tourist posing at a landmark", "road sign", "welcome sign", "national park sign", "trail marker",
        "scenic overlook", "eiffel tower", "statue of liberty", "golden gate bridge", "grand canyon",
        "old faithful", "niagara falls", "mount rushmore", "times square", "las vegas strip",
        "disney castle", "white house", "big ben", "colosseum", "atomium", "taipei 101", "great wall"],
    "Objects": [
        "flowers in a vase", "bouquet", "potted plant", "christmas ornament", "wreath", "candle", "toy",
        "lego", "stuffed animal", "doll", "toy train", "board game", "playing cards", "puzzle",
        "video game", "book", "bookshelf", "newspaper", "laptop", "desktop computer", "tablet", "smartphone",
        "television", "camera", "telescope", "microscope", "piano", "guitar", "violin", "drums", "trumpet",
        "saxophone", "microphone", "painting", "sculpture", "pottery", "quilt", "jewelry", "wristwatch",
        "ring", "glasses", "shoes", "sneakers", "cleats", "hat", "backpack", "handbag", "umbrella",
        "sports jersey", "uniform", "costume", "medal", "trophy", "ribbon", "certificate", "flag",
        "american flag", "banner", "furniture", "sofa", "bed", "crib", "high chair", "bunk bed", "desk",
        "fireplace", "chandelier", "lamp", "clock", "mirror", "tools", "lawn mower", "snow shovel",
        "garden hose", "wheelbarrow", "grill", "cooler", "kite", "ball", "frisbee", "hula hoop",
        "jump rope", "sandcastle", "snowman", "snow fort", "sled", "skis", "surfboard", "fishing rod",
        "tent gear", "money", "coins", "gift box", "greeting card", "sign"],
    "Documents & screens": [
        "screenshot of a phone screen", "screenshot of a website", "text document", "handwritten note",
        "letter", "receipt", "invoice", "form", "ticket", "boarding pass printout", "menu", "recipe",
        "book page", "newspaper clipping", "magazine cover", "map", "floor plan", "blueprint", "chart",
        "spreadsheet", "presentation slide", "whiteboard", "chalkboard", "calendar", "report card",
        "child's drawing", "child's schoolwork", "poster", "flyer", "business card", "id card",
        "license plate", "barcode", "qr code", "computer code", "sheet music", "meme", "comic",
        "product photo", "product label", "price tag", "scoreboard display", "tv screen"],
    "Photo style": [
        "black and white photo", "sepia toned old photo", "scanned old family photo", "faded color photo",
        "polaroid", "studio portrait with backdrop", "school picture day photo", "professional headshot",
        "close-up macro photo", "aerial view", "drone photo", "underwater photo", "panorama",
        "long exposure light trails", "silhouette", "backlit photo", "blurry photo", "dark underexposed photo",
        "overexposed photo", "photo taken at night", "photo taken through a window", "reflection in water",
        "reflection in a mirror", "shadow", "collage", "photo with a decorative frame",
        "photo with text overlay", "cartoon", "illustration", "logo", "pattern", "empty landscape with no people"],
}
NAMES = [(g, n) for g, names in VOCAB.items() for n in names]  # concept index -> (group, name)
INDEX = {n: c for c, (_, n) in enumerate(NAMES)}
MAX_LABELS = 3
MIN_PROB = 0.15
SECONDARY_RATIO = 0.6  # an extra label must be at least this likely relative to the best one
CHUNK = 4096           # photos scored at a time: the N x vocabulary table never exists in full
REBUILD_EVERY = 60     # seconds between rebuilds while the embedding job keeps growing the index

_lock = threading.Lock()
_cache = {"gen": None}


def _text_matrix():
    """Unit embeddings for the vocabulary, cached on disk per model, model revision and vocabulary."""
    engine = semantic.active
    key = hashlib.md5("|".join(n for _, n in NAMES).encode()).hexdigest()[:12]
    rev = hashlib.md5((engine.installed_revision() or "").encode()).hexdigest()[:12]  # another revision embeds text differently
    path = config.DATA_DIR / f"concepts-{engine.name}-{key}-{rev}.npy"
    if path.exists():
        return np.load(path)
    mat = engine.text_matrix([engine.cfg["prompt"].format(n) for _, n in NAMES])
    for old in config.DATA_DIR.glob(f"concepts-{engine.name}-*.npy"):  # another vocabulary or revision: never read again
        if old != path:
            old.unlink(missing_ok=True)
    tmp = path.with_suffix(".tmp.npy")
    np.save(tmp, mat)
    tmp.replace(path)
    return mat


def _build():
    """Per embedded photo, its MAX_LABELS best concepts and their softmax probabilities
    (a weak secondary label is zeroed). Rows follow the engine's id order."""
    semantic.load()
    with semantic._lock:
        ids, vecs, gen = semantic.ids, semantic.vecs, semantic.key
    if ids is None or not len(ids):
        return None
    T = _text_matrix().T  # [dim, concepts]
    n, k = len(ids), min(MAX_LABELS, T.shape[1])
    top = np.zeros((n, MAX_LABELS), np.int64)
    top_p = np.zeros((n, MAX_LABELS), np.float32)
    for s in range(0, n, CHUNK):  # a chunk of rows at a time, softmax in place: one chunk x vocabulary array, not three N x vocabulary ones
        with np.errstate(all="ignore"):  # Accelerate emits spurious float warnings on matmul
            logits = 100.0 * (vecs[s:s + CHUNK] @ T)
        logits -= logits.max(axis=1, keepdims=True)
        np.exp(logits, out=logits)
        logits /= logits.sum(axis=1, keepdims=True)
        part = np.argpartition(-logits, k - 1, axis=1)[:, :k]  # the k best of each row, unordered: cheaper than sorting the whole row
        pp = np.take_along_axis(logits, part, axis=1)
        order = np.argsort(-pp, axis=1)
        top[s:s + CHUNK, :k] = np.take_along_axis(part, order, axis=1)
        top_p[s:s + CHUNK, :k] = np.take_along_axis(pp, order, axis=1)
    weak = top_p < SECONDARY_RATIO * top_p[:, :1]   # never applies to the best label itself
    top_p = np.where(weak, 0.0, top_p)
    return {"gen": gen, "built": time.time(), "ids": ids, "top": top, "top_p": top_p, "pos": {int(i): k for k, i in enumerate(ids)}}


def _data():
    """The label table for the active engine, rebuilt when its embeddings change. While the embedding job
    is still adding to the index, at most once a minute: the previous table answers meanwhile."""
    if not enabled():
        return None
    with _lock:
        if _cache.get("gen") != semantic.key or "ids" not in _cache:
            if "ids" in _cache and semantic.running and time.time() - _cache.get("built", 0) < REBUILD_EVERY:
                return _cache
            built = _build()
            if built is None:
                return None
            _cache.clear()
            _cache.update(built)
        return _cache


def photos_for(name: str) -> list:
    """Photo ids the model labels with this concept, most confident first."""
    d = _data()
    if d is None or name not in INDEX:
        return []
    hit = (d["top"] == INDEX[name]) & (d["top_p"] >= MIN_PROB)
    rows = np.nonzero(hit.any(axis=1))[0]
    score = (d["top_p"] * hit)[rows].max(axis=1)
    return [int(d["ids"][r]) for r in rows[np.argsort(-score)]]


def matching(text: str) -> list:
    """Concept names a free-text search should also cover: the whole query is a concept name,
    or a single query word of four letters or more is a whole word of one ("beach" also finds
    "tropical beach with palm trees"). Short or multi-word queries match by name only, so "a"
    or "red car" do not pull in half the vocabulary."""
    q = " ".join(text.lower().split())
    if not q:
        return []
    out = [n for n in INDEX if n == q]
    if " " not in q and len(q) >= 4:
        out += [n for n in INDEX if n != q and q in n.replace("'", " ").split()]
    return out


def photos_matching(text: str) -> list:
    """Photo ids for every concept matching(text) covers, each once, most confident first."""
    seen, out = set(), []
    for name in matching(text):
        for pid in photos_for(name):
            if pid not in seen:
                seen.add(pid); out.append(pid)
    return out


def summary(valid_ids=None) -> list:
    """[{group, name, n, cover}] for every concept with at least one photo (among valid_ids
    when given); the cover is the photo the model is most confident about."""
    d = _data()
    if d is None:
        return []
    top, top_p = d["top"], d["top_p"]
    keep = top_p >= MIN_PROB
    if valid_ids is not None:
        keep &= np.isin(d["ids"], np.fromiter(valid_ids, np.int64))[:, None]
    # one pass over all (photo, label) hits instead of one scan of the table per concept
    rows, slots = np.nonzero(keep)
    if not len(rows):
        return []
    concept, p = top[rows, slots], top_p[rows, slots]
    n = np.bincount(concept, minlength=len(NAMES))
    order = np.lexsort((rows, -p))  # best probability first; the earliest row wins a tie, like argmax
    first = np.unique(concept[order], return_index=True)[1]
    cover = dict(zip(concept[order][first].tolist(), d["ids"][rows[order][first]].tolist()))
    return [{"group": group, "name": name, "n": int(n[c]), "cover": int(cover[c])}
            for c, (group, name) in enumerate(NAMES) if n[c]]


def for_photo(photo_id: int) -> list:
    """[{name, p}] the labels of one photo, best first."""
    d = _data()
    if d is None or photo_id not in d["pos"]:
        return []
    k = d["pos"][photo_id]
    return [{"name": NAMES[int(c)][1], "p": round(float(p), 2)}
            for c, p in zip(d["top"][k], d["top_p"][k]) if p >= MIN_PROB]


def representative(photo_ids: list, text: str = None):
    """The most typical photo of a set: closest to the set's average embedding,
    nudged toward `text` (e.g. the tag's name) when given."""
    semantic.load()
    with semantic._lock:
        ids, vecs = semantic.ids, semantic.vecs
    if ids is None or not len(ids) or not photo_ids:
        return None
    rows = np.nonzero(np.isin(ids, np.asarray(photo_ids, np.int64)))[0]
    if not len(rows):
        return None
    sub = vecs[rows]
    with np.errstate(all="ignore"):
        score = sub @ _unit(sub.mean(axis=0))
        if text:
            try:
                score = score + 2.0 * (sub @ semantic.text_vector(semantic.cfg["prompt"].format(text)))
            except Exception:  # the text model may be missing; the plain average still gives a cover
                pass
    return int(ids[rows[int(score.argmax())]])
