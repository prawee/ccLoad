# mittr-load

คู่มือ build และรัน mittr-load ใน local โดยแยก configuration และข้อมูลออกจาก ccLoad บน `master`

## ไฟล์เฉพาะ mittr

| ไฟล์ | หน้าที่ |
| --- | --- |
| `README.mittr.md` | คู่มือฉบับนี้ |
| `docker-compose.mittr.yml` | รัน local image `mittr-load:1.0.0` |
| `.env.mittr.example` | ตัวอย่าง env ที่คัดลอกจาก `.env.example` โดยคงเนื้อหาเดิม |
| `.env.mittr.local` | configuration และ secret ของเครื่องนี้ ไม่ commit และไม่เข้า Docker build context |

ใช้ Docker Desktop หรือ Docker Engine พร้อม Docker Compose v2 ขึ้นไป และ Git โดยรันคำสั่งจาก root ของ repository

## 1. เตรียม env ของ mittr

```bash
cp .env.mittr.example .env.mittr.local
chmod 600 .env.mittr.local
```

แก้ `CCLOAD_PASS` ใน `.env.mittr.local` เป็นรหัส admin ที่แข็งแรง ห้ามใช้ค่าตัวอย่าง หากรหัสมี `$` ให้ครอบด้วย single quotes

ค่าต่อไปนี้สามารถเพิ่มหรือแก้ในไฟล์ local ได้:

```dotenv
CCLOAD_PASS='replace-with-your-own-strong-password'
MITTR_PORT=8080
TZ=Asia/Bangkok
```

อย่าสร้าง `.env.mittr` เพราะชื่อดังกล่าวยังไม่ถูก ignore; ใช้ `.env.mittr.local` ซึ่งตรงกับ `.env.*.local` ในทั้ง `.gitignore` และ `.dockerignore` อยู่แล้ว ตรวจสอบได้ด้วย:

```bash
git check-ignore .env.mittr.local
```

ไม่ต้องแก้ `.env` หรือ `.env.docker` ของ deployment อื่น และไม่ควรเก็บ secret ไว้ใน `.env.mittr.example`

## 2. Build image

ใช้ Dockerfile ของ upstream โดยไม่ต้องทำสำเนา Dockerfile:

```bash
docker build \
  --build-arg VERSION=1.0.0 \
  --build-arg COMMIT="$(git rev-parse HEAD)" \
  --tag mittr-load:1.0.0 \
  .
```

คำสั่งนี้ build source ใน working tree รวมการแก้ไขที่ยังไม่ commit; ค่า `COMMIT` ระบุ HEAD เท่านั้น จึงควร build จาก working tree ที่สะอาดเมื่อต้องการอ้างอิง revision อย่างแน่นอน Dockerfile ใช้ `-tags sonic` อยู่แล้ว

Env ของ mittr ใช้ตอนรัน ไม่ใช่ตอน build: ไม่ส่ง `CCLOAD_PASS`, API token หรือ DSN ผ่าน `--build-arg` และไม่ฝัง secret ใน image ไฟล์ `.env.mittr.local` ถูกตัดออกจาก build context ด้วย `.dockerignore` เดิม

โดยปกติจะ build ตาม architecture ของ Docker engine เช่น `linux/arm64` บน Apple Silicon หากเครื่องปลายทางเป็น x86-64 ให้ใช้:

```bash
docker buildx build \
  --platform linux/amd64 \
  --build-arg VERSION=1.0.0 \
  --build-arg COMMIT="$(git rev-parse HEAD)" \
  --tag mittr-load:1.0.0 \
  --load \
  .
```

การ build architecture อื่นอาจต้องใช้ emulation ที่ Docker engine รองรับ ทั้งสองคำสั่งไม่ push image ไป registry

## 3. ตรวจสอบและเริ่มทำงาน

```bash
docker compose --env-file .env.mittr.local -p mittr-load -f docker-compose.mittr.yml config --quiet

docker compose --env-file .env.mittr.local -p mittr-load -f docker-compose.mittr.yml up -d --wait
```

เปิด `http://localhost:8080` หรือ port ที่ตั้งใน `MITTR_PORT` และเข้าสู่ระบบด้วย `CCLOAD_PASS` จากนั้นสร้าง channel และ API token ผ่านหน้าเว็บ

Compose ใช้ `pull_policy: never` จึงต้อง build image ในเครื่องก่อน หากเปลี่ยน image version ต้องแก้ tag ใน `docker-compose.mittr.yml` ให้ตรงกันด้วย

### Env ถูกอ่านอย่างไร

- `--env-file .env.mittr.local` ใช้แทนค่า `${CCLOAD_PASS}`, `${MITTR_PORT}` และ `${TZ}` ใน Compose โดยไม่ใช้ `.env` เป็น interpolation file ตามปกติ
- `env_file` ของ service ส่งตัวแปรจาก `.env.mittr.local` เข้า container เช่น `CCLOAD_API_TOKENS`, ตัวเลือก database และ cache
- `environment` ใน Compose มีลำดับความสำคัญเหนือ `env_file` โดยตั้ง `PORT=8080`, `SQLITE_PATH=/app/data/ccload.db`, `GIN_MODE=release`, `CCLOAD_CONTAINER=1` และ `CCLOAD_ANTHROPIC_CLI_VERSION_SYNC=false` ไว้สำหรับ deployment นี้ ดังนั้นค่าชื่อเดียวกันใน env จะไม่ทับค่าดังกล่าว
- ตัวแปรที่ export ไว้ใน shell มีลำดับความสำคัญเหนือไฟล์เมื่อ Compose ทำ interpolation โดยเฉพาะ `CCLOAD_PASS`, `MITTR_PORT` และ `TZ`; unset ค่าที่ไม่ต้องการก่อนรัน
- `PORT` คือ port ภายใน container ที่คงเป็น 8080; ใช้ `MITTR_PORT` เปลี่ยน port ของ host
- ตัวอย่างใช้ SQLite เป็นค่าเริ่มต้น หากเปิด MySQL/PostgreSQL ใน env ต้องจัดเตรียม database เอง; `127.0.0.1` ใน container ไม่ใช่ host

ตัวอย่าง env เดิมมีข้อความแนะนำให้ copy เป็น `.env` เพื่อคงเนื้อหาต้นฉบับไว้ สำหรับ mittr ให้ใช้ชื่อ `.env.mittr.local` ตามคู่มือนี้แทน

## 4. ตรวจสอบสถานะและดู log

```bash
docker compose --env-file .env.mittr.local -p mittr-load -f docker-compose.mittr.yml ps

docker compose --env-file .env.mittr.local -p mittr-load -f docker-compose.mittr.yml logs --tail 100 -f

curl --fail http://localhost:8080/health
```

หากเปลี่ยน `MITTR_PORT` ให้ปรับ URL ของ curl ตามนั้น อย่าแชร์ผล `docker compose config` แบบเต็มหรือ `docker inspect` เพราะอาจมี secret

## 5. Rebuild และหยุดทำงาน

หลังแก้ source ให้ build ใหม่ด้วยคำสั่งในขั้นตอน 2 แล้วแทนที่ container:

```bash
docker compose --env-file .env.mittr.local -p mittr-load -f docker-compose.mittr.yml up -d --force-recreate --wait
```

หยุดและลบ container/network โดยเก็บข้อมูลไว้:

```bash
docker compose --env-file .env.mittr.local -p mittr-load -f docker-compose.mittr.yml down
```

ข้อมูล SQLite อยู่ใน named volume `mittr-load_mittr-data` เมื่อใช้ project name ตามคำสั่งข้างต้น ห้ามใช้ `down --volumes` หรือ `down -v` หากต้องการเก็บข้อมูล การเปลี่ยน project name จะใช้ volume คนละชุด

## 6. รับการเปลี่ยนแปลงจาก master

การปรับแต่ง deployment ของ mittr อยู่ในไฟล์ชื่อเฉพาะ ไม่แก้ `README.md`, `.env.example`, `.gitignore`, `.dockerignore`, `Dockerfile`, `Makefile`, Compose เดิม หรือ release workflow จึงลดโอกาสชนกับ upstream แต่ไม่รับประกันว่า cherry-pick ทุก commit จะไม่มี conflict หรือไม่มีการเปลี่ยนพฤติกรรม

ก่อนรวม commit ให้ตรวจ working tree และ commit การเปลี่ยนแปลงที่ต้องการเก็บตาม workflow ของทีม ห้าม stage `.env.mittr.local`

```bash
git status --short
git fetch origin
git show --stat <commit-from-master>
git cherry-pick <commit-from-master>
```

หลัง cherry-pick:

1. ตรวจว่า env contract, Dockerfile, `.gitignore` และ `.dockerignore` ของ upstream เปลี่ยนหรือไม่ โดยเฉพาะการ exclude `.env.*.local` และ build args `VERSION`/`COMMIT`
2. คงชื่อ Compose project เป็น `mittr-load` เพื่อใช้ volume เดิม
3. Validate Compose, build image ใหม่ และ recreate ตามขั้นตอนข้างต้น
4. ตรวจ `/health`, admin login และ proxy ด้วย channel ทดสอบก่อนนำไปใช้งานจริง

หาก commit จาก master แตะไฟล์ `*.mittr.*` หรือเปลี่ยน runtime contract อาจต้อง resolve conflict หรือปรับ mittr config ด้วยตนเอง ไม่ควรแก้ application env prefix จาก `CCLOAD_*` เป็น `MITTR_*` เพราะ binary ยังอ่านชื่อเดิม

## ขอบเขตการตรวจสอบ image 1.0.0

การทดสอบ local ก่อนเพิ่มคู่มือนี้ผ่าน 60 รายการด้วย mock upstream ครอบคลุม authentication, channel/token CRUD, OpenAI/Anthropic/Responses แบบ JSON และ SSE, failover, RPM/concurrency limits, usage/cost และ persistence หลัง recreate โดยไม่ได้ทดสอบ provider จริง, OAuth, Gemini, WebSocket, browser interaction หรือ load test ผลดังกล่าวไม่ทดแทนการทดสอบซ้ำหลังรวม source จาก master
