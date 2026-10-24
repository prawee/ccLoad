# mittr-load

คู่มือ build และรัน mittr-load ใน local โดยแยก configuration และข้อมูลออกจาก ccLoad บน `master`

## ไฟล์เฉพาะ mittr

| ไฟล์ | หน้าที่ |
| --- | --- |
| `README.mittr.md` | คู่มือฉบับนี้ |
| `docker-compose.mittr.yml` | รัน local image `mittr-load:1.0.0` |
| `.env.mittr.example` | ตัวอย่าง env ที่คัดลอกจาก `.env.example` โดยคงเนื้อหาเดิม |
| `docker-compose.mittr-postgres.yml` | PostgreSQL แบบ optional แยก project จาก app |
| `.env.mittr-postgres.example` | ตัวอย่าง env สำหรับ PostgreSQL เท่านั้น |
| `.env.mittr-postgres.local` | secret ของ PostgreSQL ไม่ commit และไม่เข้า Docker build context |
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
- `environment` ใน Compose มีลำดับความสำคัญเหนือ `env_file` โดยตั้ง `PORT=8080`, `SQLITE_PATH=${SQLITE_PATH:-/app/data/ccload.db}`, `GIN_MODE=release`, `CCLOAD_CONTAINER=1` และ `CCLOAD_ANTHROPIC_CLI_VERSION_SYNC=false` ไว้สำหรับ deployment นี้ ดังนั้นค่าชื่อเดียวกันใน env จะไม่ทับค่าดังกล่าว
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

ข้อมูล SQLite และ credential อยู่ใน bind mount `./data:/app/data` ไม่ใช่ named volume โดยค่าเริ่มต้น SQLite คือ `/app/data/ccload.db`; Compose ปัจจุบันรองรับการแทนค่า `SQLITE_PATH` จึงต้องตรวจว่าอยู่ภายใน `/app/data` เสมอ อย่าลบ `./data` และอย่ารันหลาย project ที่ใช้ data directory เดียวกัน

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
2. คงชื่อ Compose project เป็น `mittr-load` และ bind mount `./data:/app/data` เดิม
3. Validate Compose, build image ใหม่ และ recreate ตามขั้นตอนข้างต้น
4. ตรวจ `/health`, admin login และ proxy ด้วย channel ทดสอบก่อนนำไปใช้งานจริง

หาก commit จาก master แตะไฟล์ `*.mittr.*` หรือเปลี่ยน runtime contract อาจต้อง resolve conflict หรือปรับ mittr config ด้วยตนเอง ไม่ควรแก้ application env prefix จาก `CCLOAD_*` เป็น `MITTR_*` เพราะ binary ยังอ่านชื่อเดิม

## 7. สำรองและกู้คืน SQLite แบบ offline

ใช้ `scripts/mittr_sqlite_backup.py` (Python 3.9+ standard library, macOS/Linux และ Compose v2 ที่รองรับ JSON config/`up --wait`) สำหรับ standalone SQLite เท่านั้น ไม่รองรับ MySQL/PostgreSQL, replica, named volume, symlink, hardlink, special file หรือหลาย container ของ service เดียวกัน ต้องมี `./data` อยู่แล้วและมีสิทธิ์อ่านข้อมูล/เขียน parent directory

ตัวอย่างต่อไปนี้เป็นคำสั่งสำหรับผู้ดูแล ไม่ใช่ขั้นตอนที่รันอัตโนมัติ สร้างปลายทางไว้นอก data directory และ repository แล้วระบุอย่างชัดเจน:

```bash
mkdir -m 700 "$HOME/mittr-backups"
python3 scripts/mittr_sqlite_backup.py backup --destination "$HOME/mittr-backups"
```

คำสั่งแสดง path ของ `mittr-<UTC timestamp>-<random>/snapshot` เมื่อสำเร็จ สำรองทั้ง data directory รวม credential, SQLite และ sidecar หลังหยุด app แล้วเท่านั้น ใช้ directory `0700`, file `0600`, SHA-256 manifest ครบทุกไฟล์และ SQLite `PRAGMA integrity_check` ก่อนเผยแพร่ snapshot ด้วย atomic rename โดยไม่เขียนทับ backup เดิม ไฟล์ `.env.mittr.local` **ไม่ถูกเพิ่ม** เข้า backup; อย่าเก็บ env ไว้ใน data เพราะทุกไฟล์ใน data จะถูกสำรอง

ใช้ path ที่คำสั่ง backup แสดงแทน `BACKUP_PATH`:

```bash
python3 scripts/mittr_sqlite_backup.py verify --backup BACKUP_PATH
python3 scripts/mittr_sqlite_backup.py restore --backup BACKUP_PATH --confirm-restore
```

- `verify` ไม่เรียก Docker และไม่แก้ backup; ตรวจ inventory/checksum/integrity โดยเปิด SQLite บน temporary private copy เพื่อรองรับ WAL โดยไม่เปลี่ยน snapshot
- `backup`/`restore` ใช้ค่าเริ่มต้น `--data ./data --env-file .env.mittr.local --compose-file docker-compose.mittr.yml --project mittr-load --service app`; เปลี่ยนแต่ละค่าได้ผ่าน flag โดย path อ้างอิง working directory และไม่รับ `..` หรือ symlink ใน path
- ก่อน stop ตรวจ JSON Compose config และ mount/env ของ container เดิมว่าตรงกับ data directory และ SQLite path จริง ยอมรับเพียง writable bind mount เดียวที่ `/app/data`; unset `CCLOAD_PASS`, `MITTR_PORT`, `TZ` ใน subprocess เพื่อใช้ interpolation file และไม่แสดง Docker output ที่อาจมี secret หากเปลี่ยน `--env-file` ต้องปรับ `env_file` ใน Compose ให้ตรงด้วย
- ใช้ nonblocking `flock` ใน parent ของ data ร่วมกันระหว่าง backup/restore อย่าลบ lock file หรือเปลี่ยน path ระหว่างทำงาน ผู้ดูแลต้องกัน service/โปรแกรมอื่นเขียนข้อมูลหรือแก้ path/backup ระหว่างทำงาน; lock ไม่ควบคุม Docker หรือ writer ภายนอก
- ถ้า app เดิมกำลังรันจะ stop แล้ว start ด้วย `up -d --no-deps --force-recreate --wait` (timeout 120 วินาที) เพื่อ bind directory inode ใหม่หลัง restore; ถ้าเดิมหยุดอยู่จะไม่ start การสำรองล้มเหลวยังพยายาม restart และรายงานทั้งสอง failure ถ้ามี
- Restore ต้องระบุ `--confirm-restore` ตรวจ backup และ staging copy ให้ครบ **ก่อน stop** จากนั้นใช้ local app image (`--pull=never`) เป็น root helper แบบไม่มี network เพื่อ `chown -R 1001:1001` เฉพาะ staging data ไม่เพิ่ม permission ของ secret บน Linux ผู้รันต้องมีสิทธิ์อ่าน UID 1001 เช่นผ่านสิทธิ์ผู้ดูแล; บน Docker Desktop ownership ของ bind mount ขึ้นกับ file sharing ของเครื่อง ต้องทดสอบ recovery ใน deployment แยกก่อนใช้จริง
- Restore เก็บ data เดิมด้วย rename ใต้ sibling `.<data-name>.mittr-stage-<random>/original` ไม่ลบหรือเขียนทับ หาก start/health check ล้มเหลวจะ stop restored app, ย้ายข้อมูลที่ล้มเหลวเป็น `failed-restored`, คืน original แล้ว restart หาก stop เพื่อ rollback ไม่สำเร็จจะไม่สลับข้อมูลขณะ app อาจยังรัน และแสดงตำแหน่ง original สำหรับผู้ดูแล
- SIGINT/SIGTERM พยายามใช้ recovery path เดียวกัน; SIGKILL, ไฟดับ, filesystem failure, interrupt ซ้ำระหว่าง recovery และการเปลี่ยนไฟล์จากภายนอกไม่รับประกัน auto-recovery ตรวจ recovery path ที่รายงานก่อนทำอะไรต่อ ไม่มีการลบ original, failed restore หรือ `.partial` อัตโนมัติ ต้องจัดการ retention เอง ตรวจว่ามีพื้นที่พอสำหรับ backup/staging และ temporary integrity copies

Backup มี API key/OAuth credential และข้อมูลส่วนบุคคล แม้ permission จำกัดก็ **ไม่ได้เข้ารหัส** ต้องเข้ารหัสก่อนย้ายไป storage อื่นและจัดการกุญแจแยกต่างหาก สำรอง env/secret แยกด้วยการเข้ารหัส ห้าม commit backup หรือ env; SHA-256 ตรวจความเสียหาย ไม่ใช่ลายเซ็นรับรอง backup ที่ไม่น่าเชื่อถือ ใช้เฉพาะ backup และ parent directory ที่เชื่อถือได้

Fixture-only tests (สร้าง SQLite ใน temporary directory, fake Compose runner, ห้าม subprocess จริง):

```bash
python3 -B -m unittest scripts.test_mittr_sqlite_backup -v
```

Docker integration tests (opt-in, Python standard library `unittest`):

```bash
MITTR_DOCKER_INTEGRATION=1 python3 -B -m unittest scripts.test_mittr_sqlite_backup_integration -v
```

ต้องมี Docker daemon, Compose ที่รองรับ `up --wait` และ local image `mittr-load:1.0.0` อยู่แล้ว ไม่ build/pull image หากไม่ตั้ง `MITTR_DOCKER_INTEGRATION=1` จะ skip ทั้ง 5 integration tests โดยไม่เรียก Docker

- สร้าง Compose file, env/password/token สังเคราะห์และ SQLite ใน system temporary directory เท่านั้น แต่ละ test ใช้ project `mittr-sqlite-integration-<random>` และ bind mount เดียวที่ `/app/data` รัน app จริง UID/GID `1001:1001` ให้ทำ migrations ก่อนสำรอง ไม่มี host port, `network_mode: none` และปิด version sync
- ทดสอบ backup/verify/restore จริงพร้อม `/health` ผ่าน `docker compose exec ... wget`, preserved original, private snapshot permissions, ownership helper, stopped-state preservation, snapshot uniqueness และการปฏิเสธ publish ทับ directory เดิมโดยไม่เรียก lifecycle
- Fault injection มีเฉพาะ Python `copy_private` ที่โยน `OSError` ระหว่าง snapshot เพื่อพิสูจน์ว่า app restart ได้; rollback ใช้ backup ที่สร้างอย่างถูกต้องจากข้อมูลสังเคราะห์ขณะหยุด app พร้อมไฟล์ `fail-start` ทำให้ restored app เริ่มไม่สำเร็จ แล้วตรวจว่า original กลับมา healthy และเก็บ `failed-restored` ไว้ ไม่มี fake runner สำหรับ lifecycle และไม่แก้ manifest/checksum เพื่อจำลอง backup
- Test subclass ลด `up --wait-timeout` เป็น 8 วินาทีเท่านั้น ไม่เปลี่ยน timeout 120 วินาทีของเครื่องมือจริง โดยปกติ suite ใช้เวลาประมาณ 2 นาที ขึ้นกับ Docker daemon
- ไม่อ่าน `.env.mittr.local` หรือ `./data` จริง ไม่เรียก lifecycle ของ project จริง ไม่ใช้ named volume; อ่านเฉพาะ container ID/state ของ project `mittr-load` ด้วย label-filtered `docker ps` ก่อน/หลังเพื่อยืนยันว่าไม่เปลี่ยน
- Cleanup เรียก `down` เฉพาะ generated project และใช้ local image helper ไม่มี network เพื่อคืนสิทธิ์เฉพาะ temporary fixture ก่อนลบ ตรวจว่า temporary directory ถูกลบและ generated projects เหลือ container/volume/network เป็นศูนย์ ไม่ใช้ prune หรือแก้ permission ของข้อมูลจริง

ข้อจำกัด: ทดสอบกับ local image และ Docker/filesystem ของเครื่องที่รัน ไม่ทดแทนการตรวจ Linux UID permissions บน host อื่น, provider จริง, SIGKILL/ไฟดับ หรือ storage failure จริง

## 8. PostgreSQL แบบ optional (ไม่เปลี่ยน SQLite เดิม)

`docker-compose.mittr-postgres.yml` รันเฉพาะ PostgreSQL แยก project `mittr-postgres` ไม่รวมกับ Compose ของ app และไม่อ่าน `.env.mittr.local` ใช้ `postgres:18-alpine` ตาม CI ใน `.github/workflows/test.yml` โดยเก็บข้อมูล PostgreSQL 18 ที่ `/var/lib/postgresql` ผ่าน named volume `mittr-postgres_postgres-data` ไม่แตะ `./data`

เตรียม env แยกต่างหาก:

```bash
cp .env.mittr-postgres.example .env.mittr-postgres.local
chmod 600 .env.mittr-postgres.local
```

เปลี่ยน `MITTR_POSTGRES_PASSWORD` เป็นรหัสผ่านที่แข็งแรง ห้ามใช้ placeholder; หากมี `$` ให้ครอบด้วย single quotes ไฟล์ local ตรงกับ `.env.*.local` ที่ ignore อยู่แล้วทั้ง Git และ Docker build context ค่าเริ่มต้น database/user คือ `mittr` และเปิดเฉพาะ `127.0.0.1:5433` เพื่อไม่ชน PostgreSQL อื่นบน port 5432 เปลี่ยนได้ด้วย `MITTR_POSTGRES_PORT` ตัวแปรที่ export ใน shell มีลำดับความสำคัญเหนือ env file เช่นกัน

คำสั่งต่อไปนี้เป็น opt-in สำหรับผู้ดูแล ไม่ต้องรันหากยังใช้ SQLite อย่างเดียว:

```bash
docker compose --env-file .env.mittr-postgres.local -p mittr-postgres -f docker-compose.mittr-postgres.yml config --quiet

docker compose --env-file .env.mittr-postgres.local -p mittr-postgres -f docker-compose.mittr-postgres.yml up -d --wait

docker compose --env-file .env.mittr-postgres.local -p mittr-postgres -f docker-compose.mittr-postgres.yml ps

docker compose --env-file .env.mittr-postgres.local -p mittr-postgres -f docker-compose.mittr-postgres.yml logs --tail 100 -f
```

Compose ปฏิเสธ password ที่ไม่ได้ตั้งหรือเป็นค่าว่าง healthcheck ใช้ `pg_isready` ตรวจความพร้อมของ server ไม่ใช่การยืนยัน password หรือทดสอบ app connection อย่าแชร์ผล `config` แบบเต็มเพราะมี secret

หยุดโดยเก็บ volume เดิม:

```bash
docker compose --env-file .env.mittr-postgres.local -p mittr-postgres -f docker-compose.mittr-postgres.yml down
```

ห้ามใช้ `down -v`/`down --volumes` หากต้องการเก็บข้อมูล และคง project name เดิมเพื่อใช้ volume เดิม ค่า `POSTGRES_DB`/`POSTGRES_USER`/password ใช้ initialize volume ว่างเท่านั้น การแก้ env ไม่เปลี่ยน database/user/password ใน volume ที่มีข้อมูล ต้องจัดการด้วย SQL และขั้นตอน rotation ที่เหมาะสม ไม่ลบ volume เพื่อเปลี่ยนรหัสผ่าน ไม่เปลี่ยน major version บน volume เดิมโดยไม่มีแผน upgrade/backup

### เชื่อมต่อ app เมื่อเลือกเปลี่ยน database เองเท่านั้น

การเริ่ม PostgreSQL **ไม่สลับ app ออกจาก SQLite** และไม่มีการย้ายข้อมูล SQLite ไป PostgreSQL อัตโนมัติ หากเลือกสลับภายหลัง ต้องวางแผน migration/backup และทดสอบก่อน; database ใหม่จะไม่มี configuration/history เดิม

บนเครื่อง host ใช้ `127.0.0.1:5433` ส่วน app ใน Docker Desktop บน macOS/Windows ใช้ `host.docker.internal:5433` ตัวอย่างต่อไปนี้ให้เพิ่มเองใน `.env.mittr.local` **เฉพาะเมื่อตั้งใจเปลี่ยน backend** แล้ว recreate app ตามขั้นตอน 5:

```dotenv
CCLOAD_POSTGRES='postgres://mittr:replace-with-url-encoded-password@host.docker.internal:5433/mittr?sslmode=disable'
```

แทน password ด้วยค่าจริงที่ percent-encode ตามรูปแบบ URL และปรับ database/user/port ให้ตรงกับ PostgreSQL; ห้ามตั้ง `CCLOAD_MYSQL` พร้อมกัน และอย่าตั้ง `CCLOAD_ENABLE_SQLITE_REPLICA=1` หากต้องการ pure PostgreSQL app Compose ส่ง DSN ผ่าน `env_file: .env.mittr.local` อยู่แล้ว ไม่ส่ง env ของ PostgreSQL ทั้งไฟล์เข้า app

ชื่อ service `postgres` ไม่ resolve ข้ามสอง project และ `127.0.0.1` ใน app container คือ app เอง บน Linux Docker Engine ไม่มีเส้นทาง `host.docker.internal` อัตโนมัติ; การเพิ่ม host-gateway อย่างเดียวอาจยังเข้า port ที่ bind loopback ไม่ได้ ต้องออกแบบ shared network/เส้นทางเชื่อมต่อแยกก่อน ไม่เปิด bind เป็น `0.0.0.0` เพื่อแก้ปัญหาโดยไม่ประเมินความปลอดภัย `sslmode=disable` ในตัวอย่างมีไว้สำหรับ local development นี้เท่านั้น

สำรอง PostgreSQL ด้วยเครื่องมือ PostgreSQL เช่น `pg_dump`/`pg_restore` และทดสอบกู้คืนแยกต่างหาก **ห้ามใช้ `scripts/mittr_sqlite_backup.py` กับ PostgreSQL** สำรอง env/secret แบบเข้ารหัสแยกจาก database; named volume ไม่ใช่ backup

## 9. PostgreSQL backup/restore rehearsal แบบแยกชั่วคราว

ใช้ Python 3.9+ standard library และ local Docker image เดียวกับ source โดยไม่ build/pull image ไม่อ่านไฟล์ secret env และไม่ใช้ Compose lifecycle:

```bash
python3 -B scripts/mittr_postgres_rehearsal.py --help
python3 -B scripts/mittr_postgres_rehearsal.py --run
```

หากไม่มี `--run` จะแสดง help เท่านั้น ค่าเริ่มต้นคือ source `mittr-postgres-postgres-1` และ protected app `mittr-load-app-1`; เปลี่ยนได้ด้วย `--source-container` / `--protected-container` ต้องเป็นคนละ container และห้ามใช้ namespace `mittr-pg-rehearsal-` ไม่รับ SQL, database name หรือ credential ผ่าน CLI ใช้เพียง database เดียวตาม `POSTGRES_DB`/`POSTGRES_USER` ของ source container ที่ initialize ไว้จริง ต้องตั้งทั้งสองค่าและ `POSTGRES_PASSWORD` ให้ตรงกับ database ปัจจุบัน

- Source มีเพียง allowlisted `docker inspect`, shell configuration check, `pg_dump`, catalog introspection และ hardcoded SELECT; password ถูกใช้ผ่าน `PGPASSWORD` ภายใน container shell เท่านั้น ไม่ส่งออก host ไม่แสดง env/SQL/data/raw Docker errors ทุก source PostgreSQL connection บังคับ `PGOPTIONS` เป็น `default_transaction_read_only=on`, statement timeout 120 วินาที และ lock timeout 5 วินาที ใช้ local socket ไม่อ่าน SQL จาก dump กลับเข้า source และปฏิเสธ standby/recovery ไม่มี stop/start/restart/restore/write บน source หรือ app
- บันทึก ID, image, start time และ restart count ของ source/app ก่อน/หลัง รวมถึงต้อง running/stable และ healthy หากมี healthcheck แม้การทดสอบล้มเหลวก็ตรวจอีกครั้ง ไม่ทดแทน application-level health/functional test
- Dump แบบ custom ลง system temporary directory `0700`, file `0600` ตรวจ SHA-256 ก่อน stream เข้า `pg_restore --exit-on-error --no-owner --no-acl --dbname=...` เฉพาะ target ไม่มี persistent backup output
- Target ใช้ image ID ของ source, random password ผ่าน subprocess environment, ชื่อและ ownership label แบบสุ่ม, `--network=none`, ไม่มี published ports/bind mounts และ tmpfs `/var/lib/postgresql` จำกัด 1 GiB ใช้ทรัพยากรเฉพาะ run จึงรันซ้ำ/พร้อมกันได้โดยไม่ต้อง lock; ไม่มี shared output path ตรวจ ID/name/ownership label และห้าม protected IDs ก่อน `docker rm -f -v` ไม่ใช้ prune
- เทียบ `pg_dump --schema-only --no-owner --no-acl` หลังตัดเฉพาะ random `\\restrict`/`\\unrestrict` lines พร้อม relation inventory (รวม empty tables, partitions, views, materialized views), จำนวนแถว, canonical JSON ของทุก table/materialized-view row ที่ sort SHA-256 ใน Python, sequence `last_value/is_called` และ large-object pages การเรียงแถวไม่มีผลแต่ duplicate rows มีผล ไม่ query view functions โดยตรง ปฏิเสธ foreign tables ไม่กล่าวอ้างว่ากู้ external data ได้
- ตรวจ schema/data ก่อน dump, หลัง dump และหลัง restore พร้อมเทียบ `pg_current_wal_lsn()` ต้องเท่าเดิมตลอดช่วง เพื่อ fail เมื่อพบ source drift แทนรายงาน restore mismatch ที่ไม่น่าเชื่อถือ **ไม่ได้ใช้ coordinated exported snapshot** ต้องเลือก quiet window; WAL เป็น cluster-wide จึงอาจ fail จาก database อื่น/checkpoint/background activity แม้ app DB ไม่เปลี่ยน และ fingerprints ไม่ใช่ atomic snapshot การเปลี่ยนแล้วคืนค่าเดิมหรือ concurrent transaction ที่ยังไม่ commit อยู่นอกหลักฐานนี้ ไม่ได้หยุด writer ให้เอง
- สร้าง synthetic schema/table/identity sequence/JSON/bytea/view/materialized view เฉพาะ target database `fixture`, dump แล้ว restore เข้า target database `roundtrip` อีกครั้งทุก run หาก source ไม่มี table/row จะรายงานชัดเจนว่า real application-data recovery ยังไม่ถูกพิสูจน์ แม้ synthetic roundtrip ผ่าน
- ใช้ `try/finally` รวม SIGINT/SIGTERM; ระหว่าง cleanup ignore interrupt ซ้ำเพื่อให้ cleanup มีโอกาสจบ Docker subprocess แต่ละคำสั่ง timeout 180 วินาที readiness window 60 วินาที (คำสั่งที่กำลังรันยังถูกจำกัดด้วย subprocess timeout) Cleanup failure ไม่กลบ original failure และแสดงเฉพาะชื่อ/ID resource ให้ผู้ดูแลตรวจ label/name/ID ก่อนลบ SIGKILL, host/daemon crash, permission/storage failure ไม่รับประกัน cleanup อัตโนมัติ tmpfs ไม่ใช่การรับประกันว่าจะไม่มีข้อมูลใน host swap

ข้อจำกัด: ต้องเชื่อถือ Docker daemon, source/image/schema และ host administrator; ไม่ใช่ sandbox สำหรับ dump ที่ไม่เชื่อถือได้ ใช้หน่วยความจำตามขนาด fingerprint/fixture dump จึงเหมาะกับ local rehearsal ขนาดเล็ก ไม่ copy cluster-global roles/passwords/tablespaces หรือ ACL/ownership และไม่ทดสอบ PITR, replication, encrypted archive retention หรือ app migration/connection หลัง disaster จริง Database dump มี secret/ข้อมูลส่วนบุคคล แม้ private permission ก็ **ไม่ได้เข้ารหัส** และถูกลบหลัง run ไม่ใช่ permanent archive; backup ระยะยาวต้องวางแผน encrypted storage, แยกกุญแจ/env, retention และ recovery แยกต่างหาก

Tests ที่ไม่เรียก Docker และ opt-in integration ที่สร้าง source/protected PostgreSQL สังเคราะห์เท่านั้น:

```bash
python3 -B -m unittest scripts.test_mittr_postgres_rehearsal -v
MITTR_PG_DOCKER_INTEGRATION=1 python3 -B -m unittest scripts.test_mittr_postgres_rehearsal_integration -v
```

Integration ต้องมี `postgres:18-alpine` อยู่แล้ว ไม่มี port/network/bind mount ไม่อ่าน `.env.*.local` หรือ data จริง รวม 9 tests (เพิ่ม 5 cases); หากไม่ opt-in จะ skip ทั้งหมดโดยไม่เรียก Docker ใช้เฉพาะ generated PostgreSQL containers สำหรับ source/protected fixture ไม่ inspect source/app จริง

- คง successful rehearsal สองรอบ, real target-only restore conflict, SIGTERM handler path และ injected cleanup failure ที่รายงาน ID แล้วกู้ cleanup ด้วย ownership verification
- เพิ่ม nonempty relational source ข้ามสอง schema: parent 2 rows, child 3 rows และ empty table รวมกับ fixture เดิมเป็น 4 tables, 7 base-table rows และ materialized-view อีก 2 rows; report จึงเป็น `4 tables, 9 rows` ตรวจ fingerprint จาก `Source` เทียบ restored database `rehearsal` จริง ไม่ใช่แค่ target-only `fixture`/`roundtrip`
- ตรวจ schema/data/sequence fingerprints ครบทั้ง serial/identity, Unicode, JSONB, SQL NULL, bytea, numeric precision และ timestamptz พร้อม FK catalog และ joined view; บน restored target เท่านั้นทดสอบปฏิเสธ orphan insert, referenced-parent delete, UNIQUE/CHECK violation และ trigger rejection ตรวจ next sequence values แล้วคืน sequence state เฉพาะ target
- Tamper actual temporary dump หลังบันทึก checksum ต้องได้ `Dump checksum mismatch` และไม่เรียก `pg_restore`; หลัง restore ลบ child, เปลี่ยน child โดยจำนวนแถวเท่าเดิม หรือ drop FK โดย data เท่าเดิม ต้องได้ `Restore completeness mismatch` จาก production comparison ไม่ปรับลด validation
- ทุก success/corruption failure ตรวจ source fingerprint และ source/protected IDs/start/restart state ไม่เปลี่ยนพร้อม `SELECT 1`; ตรวจ temporary dump directory ถูกลบและ exact generated ownership labels เหลือ containers/volumes/networks เป็นศูนย์ หลัง teardown ตรวจ source/protected fixtures ถูกลบด้วย ไม่ใช้ prune หรืออ่าน SQLite/app จริง

CI แยกใน `.github/workflows/mittr-postgres-rehearsal.yml` ทำงานบน `ubuntu-latest` เมื่อ pull request/push เปลี่ยนไฟล์ rehearsal tests/tool, PostgreSQL Compose/example env, README นี้ หรือ workflow เอง และสั่งผ่าน `workflow_dispatch` ได้ ใช้ Python 3.12 รัน unit tests ด้วย module `scripts.test_mittr_postgres_rehearsal` แล้ว pull เฉพาะ `postgres:18-alpine` และ opt-in integration ไม่ต้องมี image `mittr-load`, application secrets, Compose หรือ SQLite จริง สิทธิ์มีเพียง `contents: read` และไม่เก็บ checkout credentials

Fixture Docker wrapper ใน tests เพิ่ม label `io.mittr.postgres-rehearsal-test-run=$MITTR_PG_TEST_RUN_ID` ให้ source/protected/target ทุก container โดย CI ใช้ run ID + attempt + job เป็นเจ้าของแยกกัน ไม่เปลี่ยน production tool หลัง tests มี `always()` cleanup (รวม failure/cancellation ที่ runner ยังทำงานได้) เลือกเฉพาะ exact run label ร่วมกับ ownership label `io.mittr.postgres-rehearsal` แล้ว `docker rm -f -v` ไม่มี broad prune และไม่แตะ run อื่น Dump อยู่ใน dedicated directory ใต้ `runner.temp` ผ่าน `TMPDIR` และ cleanup ลบเฉพาะ directory นี้แม้ Docker cleanup fail; test failure ยังคงทำให้ job fail ไม่มี artifact/raw database logs กำหนด job timeout 15 นาทีและ integration 10 นาที แต่ runner/daemon crash หรือ force cancellation ยังไม่รับประกันว่า cleanup step จะทำงาน

ขอบเขต integration นี้ยังเป็น synthetic PostgreSQL 18 local Docker เท่านั้น ไม่พิสูจน์ production recovery, concurrent writers, host/daemon crash, SIGKILL หรือ storage failure จริง

## ขอบเขตการตรวจสอบ image 1.0.0

การทดสอบ local ก่อนเพิ่มคู่มือนี้ผ่าน 60 รายการด้วย mock upstream ครอบคลุม authentication, channel/token CRUD, OpenAI/Anthropic/Responses แบบ JSON และ SSE, failover, RPM/concurrency limits, usage/cost และ persistence หลัง recreate โดยไม่ได้ทดสอบ provider จริง, OAuth, Gemini, WebSocket, browser interaction หรือ load test ผลดังกล่าวไม่ทดแทนการทดสอบซ้ำหลังรวม source จาก master
