import re
import sys
import urllib.request

PULSE = "https://pulse.mirea.ru"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"
NOISE = [
    r"decode\(e,t\)\{let n=e instanceof G\?e:new G\(e\),r=n.__tsProtoDecodeDepth\?\?0;if\(r>=100\)throw new globalThis.Error\(`protobuf decode recursion limit exceeded`\);n.__tsProtoDecodeDepth=r\+1;try\{let e=t===void 0\?n.len:n.pos\+t,r=[A-Za-z0-9_$]+\(\);for\(;n.pos<e;\)\{let e=n.uint32\(\);switch\(e>>>3\)\{",
    r"\}if\(\(e&7\)==4\|\|e===0\)break;n.skip\(e&7\)\}return r\}finally\{n.__tsProtoDecodeDepth=r\}\}",
]


def fetch(path):
    return urllib.request.urlopen(urllib.request.Request(PULSE + path, headers={"User-Agent": UA})).read().decode(errors="ignore")


index = fetch("/")
main = fetch(re.search(r'src="(/assets/index-[^"]+\.js)"', index).group(1))
text = main + "".join(fetch("/assets/" + chunk) for chunk in sorted(set(re.findall(r"[A-Za-z0-9_-]+-[A-Za-z0-9_-]{8}\.js", main))))


def body(name, kind):
    found = re.search(r"[,;\s]" + re.escape(name) + r"=\{encode\(", text)
    if not found:
        return ""
    chunk = text[found.end() - len("encode("):][:30000]
    if kind == "encode":
        return chunk[:chunk.find("},decode(") + 1]
    start = chunk.find("decode(")
    chunk = chunk[start:chunk.find("},fromJSON(", start) + 1]
    for noise in NOISE:
        chunk = re.sub(noise, "", chunk)
    return chunk


def refs(src):
    return sorted(set(re.findall(r"([A-Za-z_$][A-Za-z0-9_$]*)\.(?:decode|encode)\(", src)) - set("WGnterioas"))


def describe(method):
    found = re.search(r"methodName:`" + re.escape(method) + r"`,service:([A-Za-z0-9_$]+)", text)
    if not found:
        print("==", method, "not found")
        return
    tail = text[found.end():found.end() + 800]
    service = re.search(r"[,;\s]" + re.escape(found.group(1)) + r"=\{serviceName:`([^`]+)`", text)
    request = re.search(r"([A-Za-z0-9_$]+)\.encode\(", tail).group(1)
    response = re.search(r"([A-Za-z0-9_$]+)\.decode\(", tail).group(1)
    print("==", method, service.group(1))
    print("-- request", body(request, "encode"))
    for ref in refs(body(request, "encode")):
        print("-- request part", ref, body(ref, "encode"))
    seen = set()

    def dump(name):
        if name in seen:
            return
        seen.add(name)
        print("-- response", name, body(name, "decode"))
        for ref in refs(body(name, "decode")):
            dump(ref)

    dump(response)


if len(sys.argv) < 2:
    print("\n".join(sorted(set(re.findall(r"methodName:`([^`]+)`", text)))))
for method in sys.argv[1:]:
    describe(method)
