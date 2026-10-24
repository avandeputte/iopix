"""The filler BLIP puts around its descriptions is trimmed, and nothing else is touched."""
import pytest

from app.ai.caption import tidy


@pytest.mark.parametrize("raw, want", [
    ("There is a man that is taking a selfie in the mountains", "a man taking a selfie in the mountains"),
    ("there are two men playing soccer on a field at night", "two men playing soccer on a field at night"),
    ("there are a lot of people that are sitting in the bleachers", "many people sitting in the bleachers"),
    ("there is a red flower that is in the middle of some green leaves", "a red flower in the middle of some green leaves"),
    ("there is a man and a woman that are standing next to a giraffe", "a man and a woman standing next to a giraffe"),
    ("they are two people playing tennis on a court with a net", "two people playing tennis on a court with a net"),
    ("they are sitting on a couch with a baby in their lap", "people sitting on a couch with a baby in their lap"),
    ("this is a picture of a dog on a couch", "a dog on a couch"),
    ("there's a dog that is in the water", "a dog in the water"),
    ("a young boy is climbing on a rock wall", "a young boy climbing on a rock wall"),
    ("there is an araffe man sitting on a bench", "a man sitting on a bench"),
    ("arafed woman in a red dress", "woman in a red dress"),
    ("araffe sitting on a bench", "someone sitting on a bench"),
    ("there is a arafed elephant in a park", "an elephant in a park"),
])
def test_filler_is_trimmed(raw, want):
    assert tidy(raw) == want


@pytest.mark.parametrize("raw", [
    "a group of young men playing soccer on a field",
    "the sky is blue",
    "several people are lined up at a buffet line",  # no participle right after "are"
    "black and white photograph of a woman smiling at the camera",
    "a close up of a flower",
])
def test_plain_descriptions_are_left_alone(raw):
    assert tidy(raw) == raw


def test_empty():
    assert tidy("") == "" and tidy(None) == ""
