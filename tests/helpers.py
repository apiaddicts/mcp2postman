import json


def flatten(items):
    for item in items:
        if "item" in item:
            yield from flatten(item["item"])
        else:
            yield item


def body(item):
    return json.loads(item["request"]["body"]["raw"])


def by_name(collection):
    return {i["name"]: i for i in flatten(collection["item"])}
