import asyncio, json, sys, time
TARGETS = [l.strip() for l in open(sys.argv[1]) if l.strip()]
OUT = sys.argv[2]
import os
CONC, TMO = int(os.environ.get("CONC",1200)), float(os.environ.get("TMO",0.8))
res = {}

async def probe(ip, port, sem, found):
    async with sem:
        try:
            r, w = await asyncio.wait_for(asyncio.open_connection(ip, port), TMO)
            found.append(port)
            w.close()
        except Exception:
            pass

async def scan(ip, hsem):
    async with hsem:
        t = time.time()
        sem, found = asyncio.Semaphore(CONC), []
        await asyncio.gather(*(probe(ip, p, sem, found) for p in range(1, 65536)))
        res[ip] = sorted(found)
        json.dump(res, open(OUT, "w"))
        print(ip, len(found), f"{time.time()-t:.0f}s", flush=True)

async def main():
    hsem = asyncio.Semaphore(3)
    await asyncio.gather(*(scan(ip, hsem) for ip in TARGETS))
    print("ALLDONE", flush=True)

asyncio.run(main())
