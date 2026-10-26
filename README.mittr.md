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

### ใช้ PostgreSQL เป็นฐานข้อมูลของ app (รวม deployment)

PostgreSQL ต้องสร้าง database/user และกำหนด network/firewall ให้ app container เชื่อมต่อได้ก่อน การเริ่ม PostgreSQL container **ไม่สลับ app ออกจาก SQLite**; ต้องตั้ง `CCLOAD_POSTGRES` ใน `.env.mittr.local` และ recreate app เอง การตั้งค่านี้เป็นเพียงการเลือก backend ไม่ได้ย้ายข้อมูลจาก SQLite: app จะสร้าง/ปรับ schema ใน PostgreSQL และเริ่มอ่านข้อมูลจากฐานนั้น ขณะที่ `data/ccload.db` ยังคงอยู่แยกต่างหาก

แก้ `.env.mittr.local` ให้มี DSN จริงหนึ่งบรรทัด (แทนค่าเดิมถ้ามี):

```dotenv
CCLOAD_POSTGRES='postgres://<user>:<url-encoded-password>@<reachable-host>:<port>/<database>?sslmode=require'
```

ใช้ host/port/database ที่ PostgreSQL อนุญาตจาก **ภายใน app container** ไม่ใช่ค่าที่ใช้จาก host โดยอัตโนมัติ; บน Docker Desktop ที่เชื่อมกับ PostgreSQL local ซึ่ง publish port `5433` ใช้ `host.docker.internal:5433` ได้ ส่วน managed/remote PostgreSQL ใช้ hostname และ port ที่ผู้ให้บริการระบุ ตรวจ firewall, TLS และ allowlist ให้ตรงกับทางเชื่อมต่อจริง ตั้ง `sslmode=require` สำหรับ deployment ที่รองรับ TLS; `sslmode=disable` ด้านล่างใช้เฉพาะ local development บนเครื่องที่เชื่อถือได้เท่านั้น

รหัสผ่านใน URL ต้อง percent-encode อักขระพิเศษ ห้ามใส่ DSN ใน command line, commit, issue หรือ log; เก็บไฟล์เป็น permission `600` และอย่าแชร์ผล `docker compose config` แบบเต็มหรือ `docker inspect` เพราะอาจมี secret ห้ามตั้ง `CCLOAD_MYSQL` พร้อมกัน หากต้องการ pure PostgreSQL อย่าตั้ง `CCLOAD_ENABLE_SQLITE_REPLICA=1`

หลังแก้ DSN ให้ validate แล้ว force-recreate app เพื่อให้ Compose อ่าน env file ใหม่ (การ restart container เดิมอย่างเดียวไม่โหลดค่าไฟล์ที่เปลี่ยน):

```bash
chmod 600 .env.mittr.local
docker compose --env-file .env.mittr.local -p mittr-load -f docker-compose.mittr.yml config --quiet
docker compose --env-file .env.mittr.local -p mittr-load -f docker-compose.mittr.yml up -d --no-deps --force-recreate --wait app
docker compose --env-file .env.mittr.local -p mittr-load -f docker-compose.mittr.yml ps
curl --fail http://localhost:${MITTR_PORT:-8080}/health
docker compose --env-file .env.mittr.local -p mittr-load -f docker-compose.mittr.yml logs --tail 100 app
```

ยืนยัน log ว่าระบุ `使用 PostgreSQL 存储（纯模式）` หรือ `使用 PostgreSQL 存储（混合模式）` ตาม config และ health check ผ่าน; ตรวจ database/table ผ่าน connection ที่ใช้ DSN เดียวกันโดยไม่พิมพ์ credential ออกมา ระวัง `ps`/`logs` ปกติแต่ละอย่างไม่ได้ยืนยันว่าใช้ database ที่ตั้งใจ หากแก้ DSN แล้ว container เดิมยังทำงานอยู่ ให้อ่าน `CCLOAD_POSTGRES` ใน container แบบปกปิด secret และเทียบเพียง host/port/database กับไฟล์

บน Docker Desktop หาก app กับ PostgreSQL local แยก Compose project กัน ชื่อ service `postgres` จะ resolve ข้าม project ไม่ได้ และ `127.0.0.1` ใน app container คือ app เอง จึงใช้ `host.docker.internal` กับ port ที่ publish บน host ได้ บน Linux Docker Engine ไม่มีเส้นทาง `host.docker.internal` อัตโนมัติ; การเพิ่ม host-gateway อย่างเดียวอาจยังเข้า port ที่ bind loopback ไม่ได้ ต้องออกแบบ shared network/เส้นทางเชื่อมต่อแยกก่อน ไม่เปิด bind เป็น `0.0.0.0` โดยไม่ประเมินความปลอดภัย

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

Integration ต้องมี `postgres:18-alpine` อยู่แล้ว ไม่มี port/network/bind mount ไม่อ่าน `.env.*.local` หรือ data จริง รวม 16 tests; หากไม่ opt-in จะ skip ทั้งหมดโดยไม่เรียก Docker ใช้เฉพาะ generated PostgreSQL containers สำหรับ source/protected fixture ไม่ inspect source/app จริง

- เฉพาะ synthetic source fixture ใช้ `autovacuum=off`, `checkpoint_timeout=1d`, `wal_level=minimal`, `max_wal_senders=0` และ `VACUUM (FREEZE, ANALYZE)` หลัง setup ก่อนบันทึก fingerprint เพื่อควบคุม catalog pruning/autoanalyze และ periodic standby WAL records ซึ่งทำให้ cluster-wide LSN เปลี่ยนแม้ข้อมูลเดิม ไม่เปลี่ยน production settings หรือ WAL drift guard; protected/restore target ใช้ settings เดิม เพิ่ม regression หน่วง 65 วินาทีข้าม background interval และทดสอบว่า UPDATE บน synthetic source ระหว่าง restore ยังถูกปฏิเสธด้วย `Source drift detected`
- คง successful rehearsal สองรอบ, real target-only restore conflict, SIGTERM handler path และ injected cleanup failure ที่รายงาน ID แล้วกู้ cleanup ด้วย ownership verification
- เพิ่ม nonempty relational source ข้ามสอง schema: parent 2 rows, child 3 rows และ empty table รวมกับ fixture เดิมเป็น 4 tables, 7 base-table rows และ materialized-view อีก 2 rows; report จึงเป็น `4 tables, 9 rows` ตรวจ fingerprint จาก `Source` เทียบ restored database `rehearsal` จริง ไม่ใช่แค่ target-only `fixture`/`roundtrip`
- ตรวจ schema/data/sequence fingerprints ครบทั้ง serial/identity, Unicode, JSONB, SQL NULL, bytea, numeric precision และ timestamptz พร้อม FK catalog และ joined view; บน restored target เท่านั้นทดสอบปฏิเสธ orphan insert, referenced-parent delete, UNIQUE/CHECK violation และ trigger rejection ตรวจ next sequence values แล้วคืน sequence state เฉพาะ target
- Tamper actual temporary dump หลังบันทึก checksum ต้องได้ `Dump checksum mismatch` และไม่เรียก `pg_restore`; หลัง restore ลบ child, เปลี่ยน child โดยจำนวนแถวเท่าเดิม หรือ drop FK โดย data เท่าเดิม ต้องได้ `Restore completeness mismatch` จาก production comparison ไม่ปรับลด validation
- ทุก success/corruption failure ตรวจ source fingerprint และ source/protected IDs/start/restart state ไม่เปลี่ยนพร้อม `SELECT 1`; ตรวจ temporary dump directory ถูกลบและ exact generated ownership labels เหลือ containers/volumes/networks เป็นศูนย์ หลัง teardown ตรวจ source/protected fixtures ถูกลบด้วย ไม่ใช้ prune หรืออ่าน SQLite/app จริง

### Automatic WAL evidence on unexpected integration failures

The test harness automatically emits one `WAL evidence:` metadata summary to stderr **before fixture cleanup** when `unittest` records an unexpected failure/error (including failed subtests, `tearDown`, or partial `setUp` when the source is available). Expected exceptions handled by `assertRaises` do not emit evidence. A baseline LSN is sampled after source creation, then refreshed after fixture setup/VACUUM and **before fingerprint reads**, so later read-induced maintenance remains in the diagnostic interval. Failures earlier in setup use the available early baseline or an unavailable marker.

Diagnostics accept only the generated source `Target` with a valid generated identity/name/token, and verify the exact inspected ID, name and ownership label before each SQL or WAL read (including a fresh check after segment selection). They sample `pg_current_wal_flush_lsn()` and derive segment names using `pg_walfile_name()` with forced read-only transactions and bounded statement/lock timeouts, then read the source container's own `$PGDATA/pg_wal` with `pg_waldump --start=... --end=... --limit=256 STARTSEG ENDSEG`. Both LSNs and segment names are strictly validated. At an exact segment boundary, the start name selects the segment containing the next byte while the end name selects the preceding segment. The production rehearsal's `pg_current_wal_lsn()` drift guard is unchanged. There is no checkpoint, WAL switch, snapshot-log request, source write, WAL copy, arbitrary SQL/path/container input, or production/app access. Each diagnostic Docker client command is limited to 5 seconds and 64 KiB combined output in memory; no raw output is written to disk. Killing a Docker CLI does **not** prove its exec child stopped. Each diagnostic exec independently runs `timeout -s KILL 4 sh -c ...` inside the container, with the shell using `exec` for psql/pg_waldump. These flags were checked against the actual image's BusyBox help; a real fixture regression verifies a timed-out `exec sleep 30` is absent from the container process list. The internal deadline also applies after a host output-cap kill. Normal fixture cleanup removes the entire owned container with `rm -f -v`, the lifecycle backstop; unavailable/broken Docker, SIGKILL or runner loss still cannot guarantee cleanup.

PostgreSQL 18's actual `pg_waldump --help` confirms `-n/--limit` is valid. The reproduced failure was directory autodetection selecting a zero-header preallocated future segment and rejecting its segment size as 0, not a corrupt active segment or invalid flag. Explicit positional segments avoid that arbitrary probe. Real Docker regressions commit synthetic UPDATE/INSERT with synchronous commit, assert baseline/end and before/after remain in the same active segment, and require decoded Heap UPDATE/HOT_UPDATE, INSERT and Transaction COMMIT records with nonzero counts and `status=ok`, without checkpoint/switch. A nested deliberate unittest failure additionally verifies the automatic hook decodes before cleanup and retains the original failure.

Only validated baseline/end LSNs, counts grouped by explicitly allowlisted resource manager/record type, and fixed status/truncation/incomplete/unparsed/unavailable markers are emitted. Each counted record must have a complete newline-terminated PostgreSQL metadata header and a record LSN within `[baseline, end)`, strictly increasing across decoded records (including unknown types). An out-of-range, duplicate or backwards LSN discards all counts and reports `status=invalid-record-lsn`; empty/malformed or unknown output on zero exit reports `status=unparsed-output`, not `ok`. Segment names must also share a nonzero timeline and be ordered. Any nonzero exit, timeout, unparsed output or truncation has `incomplete=true`; valid partial counts may still be shown on failure and never imply a completed read. Unknown record types, descriptions, row contents, SQL, credentials, raw Docker errors, inspect output, database logs and raw WAL are never printed. `records` counts only recognized metadata records, not all WAL records; `unparsed=true` means some output was suppressed. `record-limit=256` and `truncated=true` mark bounded evidence rather than a complete WAL history. Identical LSNs report `records=0 status=unchanged` without invoking `pg_waldump`.

The flushed end bounds the read to WAL already written by PostgreSQL; it does not freeze the live files or promise a stable snapshot. PostgreSQL 18's `XLogReaderValidatePageHeader` checks the expected `xlp_pageaddr` (its source explicitly identifies recycled segments as a typical mismatch), segment/block sizes and nondecreasing page timelines; record reading also checks CRCs and previous-record links. This rejects stale/replaced pages at another WAL address, rather than decoding their records as successful evidence for this interval. Page timeline checks are not an exact timeline/cluster identity attestation; the harness trusts the generated image/container and Docker daemon, and does not defend against a privileged adversary rewriting valid WAL at the same addresses. No copy or forced WAL retention is introduced.

Deterministic unit fault injection simulates a selected segment disappearing or a reader rejecting a replaced segment with a page-address error, including valid partial stdout followed by malicious stderr, malformed metadata, interval/order violations, timeout and byte caps. These are **not real recycling experiments**. A real Docker regression mocks only the bounded WAL-read result, preserves the original nested unittest failure, checks the entire captured stderr and result traceback for malicious payload leakage, verifies generated source/protected state and source fingerprints before removal, and proves all exact-owned fixture resources and dump directories are removed. The separate real active-segment decode tests remain required.

A separate `AutomaticRecyclingTests` fixture now exercises **actual PostgreSQL-driven recycling during a real diagnostic read**, not the fault injections above. Only this disposable source uses `POSTGRES_INITDB_ARGS=--wal-segsize=1` (supported by the image's `initdb`, verified with `SHOW`), `checkpoint_timeout=30s`, `checkpoint_completion_target=0`, `max_wal_size=16MB`, `min_wal_size=2MB`, and `wal_recycle=on`. An initial 26,000-row synthetic COPY spans multiple 1 MiB segments. A test-only anonymous-pipe scheduler starts the exact `FixtureWAL.report()` pg_waldump command, observes an actual decoded record, stops that exact exec PID with SIGSTOP, and verifies its stopped state and open WAL descriptor through read-only `/proc` inspection. It verifies a later selected interval segment is not the open segment. Bounded ordinary INSERT batches then create WAL pressure; PostgreSQL's automatic checkpointer alone decides when to recycle. There is no CHECKPOINT, pg_switch_wal, equivalent forcing SQL, WAL copy, filesystem rename/delete or header modification. The test requires the old later segment name to disappear **and its device/inode identity to appear under a larger WAL filename**, while the already-reading helper is still stopped/alive, before SIGCONT. Absence alone is not recycling evidence. The resumed real reader must exit 1 with a missing-file/page-address validation error, rather than succeeding, reaching the record/byte cap, or merely timing out; its actual output is fed back into the unchanged report parser and must produce `incomplete=true` without raw errors or synthetic payload leakage.

The pressure fixture is isolated from all quiet-source tests and has Docker limits of 512 MiB memory, one CPU, 128 PIDs and the existing 1 GiB tmpfs. Work is capped at 64 batches of 2,048 rows (1,920-byte payload each), a 100-second orchestration deadline, 110/115-second in-container reader/pipeline watchdogs and 64 KiB in-memory output; each workload statement has a 3-second statement timeout. Only this test's real reader adapter uses longer watchdogs to accommodate automatic checkpoints and pg_waldump's missing-file retry; normal diagnostics retain their unchanged 4-second child/5-second client deadlines and 256-record cap. No raw output is persisted or printed; successful test evidence contains only segment names, device/inode, counts, byte totals and monotonic ordering timestamps. Exact ownership-verified fixture removal is registered before creation and runs on failure as well, terminating remaining exec children and verifying zero labeled containers/volumes/networks. Local runs typically add about 9 seconds, but checkpoint scheduling and Docker/host performance vary; failure to observe actual recycling fails the test rather than skipping or substituting injected evidence. This proves a scheduled pause/resume read race, not the frequency of naturally occurring races or a stable live-WAL snapshot.

Missing/recycled WAL, a partially initialized/unreadable segment/header, concurrent recycling, or an end-of-WAL read failure can still produce `status=unavailable-or-active-eof`; this deliberately does not disclose the raw error and does not prove that no WAL activity occurred. Unflushed activity after the sampled flush LSN is outside the interval. Diagnostic errors never replace the original test failure or prevent fixture cleanup. Failures first introduced by cleanup cannot be retroactively captured after source destruction; SIGKILL, runner/daemon crash, and force cancellation may prevent any evidence. The workflow's separate `simulate_failure` step is not a failing PostgreSQL unittest and does not trigger this hook.

Retention is **CI job logs only**, under the repository's existing GitHub Actions log retention/access policy (or local stderr when run locally). No diagnostic artifact is uploaded or retained, and temporary database dumps still follow the existing private-directory cleanup. This is best-effort synthetic-fixture troubleshooting, not production auditing or PITR evidence.

CI แยกใน `.github/workflows/mittr-postgres-rehearsal.yml` ทำงานบน `ubuntu-latest` เมื่อ pull request/push เปลี่ยนไฟล์ rehearsal tests/tool, PostgreSQL Compose/example env, README นี้ หรือ workflow เอง และสั่งผ่าน `workflow_dispatch` ได้ ใช้ Python 3.12 รัน unit tests ด้วย module `scripts.test_mittr_postgres_rehearsal` แล้ว pull เฉพาะ `postgres:18-alpine` และ opt-in integration ไม่ต้องมี image `mittr-load`, application secrets, Compose หรือ SQLite จริง สิทธิ์มีเพียง `contents: read` และไม่เก็บ checkout credentials

Fixture Docker wrapper ใน tests เพิ่ม label `io.mittr.postgres-rehearsal-test-run=$MITTR_PG_TEST_RUN_ID` ให้ source/protected/target ทุก container โดย CI ใช้ run ID + attempt + job เป็นเจ้าของแยกกัน ไม่เปลี่ยน production tool หลัง tests มี `always()` cleanup (รวม failure/cancellation ที่ runner ยังทำงานได้) เลือกเฉพาะ exact run label ร่วมกับ ownership label `io.mittr.postgres-rehearsal` แล้ว `docker rm -f -v` ไม่มี broad prune และไม่แตะ run อื่น Dump อยู่ใน dedicated directory ใต้ `runner.temp` ผ่าน `TMPDIR` และ cleanup ลบเฉพาะ directory นี้แม้ Docker cleanup fail; test failure ยังคงทำให้ job fail ไม่มี artifact/raw database logs กำหนด job timeout 15 นาที, integration 10 นาที, simulation 1 นาที และ cleanup 2 นาที โดย timeout ของแต่ละ step ไม่ได้จองเวลาเพิ่มนอก job timeout; `always()` ไม่รับประกันว่า cleanup จะจบเมื่อหมดเวลา job, runner/daemon crash หรือ force cancellation

### รัน CI ปกติและจำลองล้มเหลว

ชื่อใน source YAML คือ `Mittr PostgreSQL rehearsal` แต่ ณ การตรวจครั้งนี้ GitHub ลงทะเบียน/แสดงชื่อเป็น `.github/workflows/mittr-postgres-rehearsal.yml` (workflow ID `394739590`); ค้นด้วยชื่อไฟล์นี้หากไม่พบชื่อ display name ผ่าน GitHub Actions หากมีปุ่ม **Run workflow** ให้เลือก branch `develop` และค่า boolean `simulate_failure` ตามกรณี หากไม่มีปุ่มให้ใช้ CLI ด้านล่าง ซึ่งตรวจแล้วว่า dispatch บน `develop` ได้ ไม่รับประกันว่า UI จะมีปุ่มเมื่อ workflow ยังไม่อยู่บน default branch ด้าน CLI ต้อง login `gh` และมีสิทธิ์ dispatch workflow ใน repository; branch ที่เลือกต้องมี workflow/tests รุ่นที่ต้องการตรวจ

รอบปกติ:

```bash
gh workflow run mittr-postgres-rehearsal.yml --repo prawee/ccLoad --ref develop -f simulate_failure=false
```

รอรอบปกติจบก่อนเริ่มรอบจำลอง เพราะ concurrency group ใช้ `github.workflow` + `github.ref` และตั้ง `cancel-in-progress: true` รอบใหม่ที่อยู่ group เดียวกัน (รวม push กับ manual dispatch บน branch เดียวกัน) จะยกเลิกรอบก่อนที่ยังทำงานอยู่ ส่วน pull request ใช้ PR ref จึงไม่ใช่ group เดียวกับ branch ref โดยอัตโนมัติ

รอบจำลองล้มเหลว:

```bash
gh workflow run mittr-postgres-rehearsal.yml --repo prawee/ccLoad --ref develop -f simulate_failure=true
```

โหมดนี้รัน unit/integration ตามปกติก่อน แล้ว step `Simulate failure with owned fixture` สร้างและเปิด container ชั่วคราวที่ติด ownership/run labels โดยไม่มี network/port ใช้ tmpfs และจงใจจบด้วย exit code 1 เพื่อทดสอบ cleanup นอก test teardown ไม่แก้ production script และไม่ลดเกณฑ์ผ่านของ tests หาก tests ก่อนหน้านี้ไม่ผ่าน step จำลองจะถูกข้าม จึงต้องตรวจว่า failure เกิดจาก step จำลองจริง

| จุดตรวจ | รอบปกติ | รอบจำลองล้มเหลว |
| --- | --- | --- |
| `Run unit tests` | ผ่าน | ผ่าน |
| `Run isolated Docker integration tests` | ผ่าน | ผ่าน |
| `Simulate failure with owned fixture` | ข้าม | ล้มเหลวตามตั้งใจ พร้อม log `simulated failure: owned running container ...` |
| `Clean up owned test fixtures and dumps` | ผ่าน | ผ่าน แม้ step ก่อนหน้าล้มเหลว |
| ผลรวม workflow | Success | Failure ตามตั้งใจ ไม่ใช่ test suite ล้มเหลว |

### ตรวจหลักฐาน cleanup

หา run ID ของรอบที่เพิ่งสั่ง แล้วดูสถานะและ log (แทน `RUN_ID` ด้วยเลขจริง):

```bash
gh run list --repo prawee/ccLoad --workflow mittr-postgres-rehearsal.yml --branch develop --event workflow_dispatch --limit 5
gh run watch RUN_ID --repo prawee/ccLoad --exit-status
gh run view RUN_ID --repo prawee/ccLoad --json conclusion,jobs,url
gh run view RUN_ID --repo prawee/ccLoad --log
```

`watch --exit-status` ของรอบจำลองจะคืน nonzero ตามตั้งใจ ไม่ควรสรุปว่า cleanup ล้มเหลวจาก exit status รวมเพียงอย่างเดียว ให้ดู step cleanup และหลักฐานต่อไปนี้ใน log:

- `Owned containers before cleanup: 1` ในรอบจำลองที่ tests/teardown ก่อนหน้าสำเร็จ แสดงว่ามี fixture ที่ cleanup ต้องลบจริง; รอบปกติอาจเป็น 0
- `Cleanup verified: 0 owned containers, 0 owned volumes; checked ... captured volume names` แสดงว่าตรวจซ้ำหลังลบแล้ว ไม่เหลือ container ที่ตรง labels, volume ที่จับชื่อไว้ หรือ volume ที่ตรง labels ของรอบนั้น Workflow cleanup ไม่ตรวจ networks; การตรวจ networks ที่กล่าวในส่วน integration เป็น assertion ของ test teardown ตาม ownership token แต่ละ fixture และ fixtures ใช้ `--network=none` ไม่ได้สร้าง network แยก
- `Cleanup verified: dedicated test directory absent` แสดงว่า directory ของ dump/fixture ใต้ `runner.temp` ถูกลบแล้ว แม้ขั้น Docker cleanup เกิดข้อผิดพลาด `finally` ยังพยายามลบ directory นี้ แต่ step จะไม่ผ่านหากการตรวจใดล้มเหลว

หาก cleanup step fail หรือขาดหลักฐาน ห้ามถือว่าล้างสำเร็จ ให้ตรวจ run ID/attempt และ resource labels ก่อนทำ manual cleanup; ห้ามใช้ `docker system prune` หรือคำสั่งลบกว้าง ๆ ข้อจำกัด runner/daemon crash, SIGKILL และ force cancellation ยังมีอยู่ตามที่ระบุข้างต้น

ตัวอย่างที่ตรวจบน GitHub แล้ว (เป็นผล ณ commit `4f453b3d` ไม่ใช่การรับรอง commit ถัดไป):

- [รอบปกติ 40050290215](https://github.com/prawee/ccLoad/actions/runs/40050290215): unit 33 + integration 9 ผ่าน และ cleanup ผ่าน
- [รอบจำลอง 40050413002](https://github.com/prawee/ccLoad/actions/runs/40050413002): tests ผ่าน, step จำลองล้มเหลวตามตั้งใจ, cleanup ลบ fixture 1 ตัวและยืนยัน 0 container/volume พร้อม directory ถูกลบ

### การแยกจาก production secrets

- CI ไม่อ้างอิง `secrets.*`, production environment หรือไฟล์ `.env.*.local`; ไม่ต้องสร้าง repository secret เพื่อรันชุดนี้ ห้ามเพิ่ม production DSN/API key หรือ mount data จริงลง fixture
- Source/protected PostgreSQL ใน integration เป็น container สังเคราะห์ที่สร้างใหม่ รวม credential และข้อมูลทดสอบ ไม่ใช่ `mittr-postgres-postgres-1` หรือแอป SQLite จริง CI ไม่เรียก CLI rehearsal ด้วย source ค่าเริ่มต้นที่ชี้ระบบ local
- Target ทุกตัวไม่มี network/host port หรือ bind mount ฐานจริง การ cleanup เลือก exact run ID + attempt + job ร่วม ownership label จึงไม่ลบทรัพยากรของรอบอื่น
- `gh` บนเครื่องผู้ดูแลใช้ GitHub authentication เพื่อ dispatch/อ่านผลเท่านั้น ไม่ส่ง credential ฐานข้อมูลขึ้น CI; workflow ใช้ token สิทธิ์ `contents: read` สำหรับ checkout และตั้ง `persist-credentials: false`
- ไม่ upload dump หรือ raw database logs เป็น artifact; dump ชั่วคราวไม่ได้เข้ารหัส แม้เป็นข้อมูลจำลองก็จำกัดสิทธิ์และลบหลังรัน

ขอบเขต integration นี้ยังเป็น synthetic PostgreSQL 18 local Docker เท่านั้น ไม่พิสูจน์ production recovery, concurrent writers, host/daemon crash, SIGKILL หรือ storage failure จริง

## ขอบเขตการตรวจสอบ image 1.0.0

การทดสอบ local ก่อนเพิ่มคู่มือนี้ผ่าน 60 รายการด้วย mock upstream ครอบคลุม authentication, channel/token CRUD, OpenAI/Anthropic/Responses แบบ JSON และ SSE, failover, RPM/concurrency limits, usage/cost และ persistence หลัง recreate โดยไม่ได้ทดสอบ provider จริง, OAuth, Gemini, WebSocket, browser interaction หรือ load test ผลดังกล่าวไม่ทดแทนการทดสอบซ้ำหลังรวม source จาก master
