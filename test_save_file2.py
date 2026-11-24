# 2026_11_23
# 2026_11_17
"""
Отправка файла на сервер через smbclient + запись в SCANNED (Firebird).

Имя файла на сервере = "s" + следующий SCANNED.ID (+ расширение исходника).

Схема (подтверждена триггером SCANNED_BI: явный ID не перезаписывается):
  1. Резервировать следующий ID:  GEN_ID(ID_SCANNED, 1)   [внутри транзакции]
  2. Вставить строку в SCANNED с явным ID (НЕ зафиксировать)
  3. Отправить файл по SMB
  4. Успех  -> commit
     Провал -> rollback + компенсирующий DELETE (страховка на случай,
             если сборка fdb авто-коммитит каждую команду)

Требуется файл ~/:.smbcreds:
    username=sfs
    password=...
    domain=asup
"""
import subprocess
import os
import re
import shutil
import tempfile
from datetime import datetime

import fdb

# ── Файловый сервер (SMB) ─────────────────────────────
SERVER    = "srv-sadko-db"
SHARE     = "SadkoAI"

# ── БД (Firebird) ─────────────────────────────────────
DB_HOST     = "windytown"
DB_DATABASE = r"c:\ibSadko\DataTest\rdb_kanc29.fdb"
DB_USER     = "SYSDBA"
DB_PASSWORD = "masterkey"
DB_LIBRARY  = "/usr/lib/x86_64-linux-gnu/libfbclient.so.2"
DB_CHARSET  = "WIN1251"

# ── Параметры записи в SCANNED (задайте под себя) ────
WHO      = "sfs@asup.local"   # ← аккаунт, под которым идёт разбор почты
CARD_ID  = 178                 # ← CARD.ID документа (MasterID) — реальный ID
TABLE    = "SCANNED"
GEN_NAME = "ID_SCANNED"       # генератор (из триггера SCANNED_BI)


# ─────────────────────────────────────────────────────
#  Отправка по SMB
# ─────────────────────────────────────────────────────
def upload_via_smbclient(local_path, remote_path,
                         server=SERVER, share=SHARE, auth_file=None):
    """Отправка файла по SMB, создание каталогов. True / исключение."""
    if auth_file is None:
        auth_file = os.path.expanduser("~/.smbcreds")
    if not os.path.isfile(local_path):
        raise FileNotFoundError(f"Файл не найден: {local_path}")
    if not os.path.isfile(auth_file):
        raise FileNotFoundError(f"Файл с учётными данными не найден: {auth_file}")

    parts = remote_path.replace("\\", "/").strip("/").split("/")
    dir_parts, filename = parts[:-1], parts[-1]

    # Пробелы в локальном пути — уносим файл в /tmp
    use_path = local_path
    tmp_copy = False
    print(use_path)
    print("---------------------------")
    if ' ' in local_path:
        tmp_file = os.path.join(tempfile.gettempdir(), os.path.basename(local_path))
        shutil.copy2(local_path, tmp_file)
        use_path = tmp_file
        tmp_copy = True

    try:
        
        commands = []
        for d in dir_parts:
            commands.append(f"mkdir {d}")
            commands.append(f"cd {d}")
            print('------d-----------', d)
        commands.append(f"put {use_path} {filename}")
        print(["smbclient", f"//{server}/{share}", "-A", auth_file])
        print("---------------------------")
        process = subprocess.run(
            ["smbclient", f"//{server}/{share}", "-A", auth_file],
            input="\n".join(commands) + "\n",
            capture_output=True, text=True
        )
        out = (process.stdout + "\n" + process.stderr).strip()
        print("Вывод smbclient:", out if out else "(пусто)")
        
        low = out.lower()
        # Успешный put всегда печатает "putting file ... as ..."
        put_ok = "putting file" in low
        # "mkdir существующего каталога" — не ошибка, отфильтровываем
        rest = re.sub(r"nt_status_object_name_collision", "", low)
        failed = (
            process.returncode != 0
            or not put_ok
            or "nt_status" in rest        # любые другие NT_STATUS — ошибка
        )
        if failed:
            raise Exception(f"Ошибка отправки (код {process.returncode})")
        # failed = "NT_STATUS" in out and "put" in out
        # if process.returncode != 0 or failed:
        #     print('------errorr1-----------', process.returncode, failed)
        #     raise Exception(f"Ошибка отправки (код {process.returncode})")
        # print(f"✓ Файл отправлен: //{server}/{share}/{remote_path}")
        # return True
    finally:
        if tmp_copy:
            os.unlink(use_path)


# ─────────────────────────────────────────────────────
#  Работа с SCANNED
# ─────────────────────────────────────────────────────
def build_doc_path(remote_path, base=""):
    """DOC_PATH: путь с обратными слешами; если задан base — с его префиксом."""
    rel = remote_path.replace("/", "\\").strip("\\")
    if base and rel:
        return f"{base}\\{rel}"
    return rel


def find_id_generator(con, table=TABLE):
    """Автопоиск генератора ID по BEFORE INSERT триггеру.

    Возвращает (имя_генератора или None, текст_триггера).
    """
    cur = con.cursor()
    try:
        cur.execute(
            "SELECT rdb$trigger_source FROM rdb$triggers "
            "WHERE upper(rdb$relation_name) = ? AND rdb$trigger_type = 1",
            (table.upper(),)
        )
        row = cur.fetchone()
    finally:
        cur.close()

    src = ""
    if row and row[0]:
        src = row[0].decode("utf-8", "replace") if isinstance(row[0], bytes) else row[0]

    m = re.search(r"GEN_ID\s*\(\s*['\"]?(\w+)['\"]?\s*,\s*1\s*\)", src, re.IGNORECASE)
    return (m.group(1) if m else None), src


def _ensure_rolled_back(con, table, new_id):
    """Откатить транзакцию; страховкой — удалить строку, если она уже
    зафиксирована (на случай, если сборка fdb авто-коммитит команды)."""
    try:
        con.rollback()
        print(f"  ⏪ Rollback выполнен (ID={new_id})")
    except Exception as e:
        print(f"  ⚠ Ошибка rollback: {e}")
    try:
        cur = con.cursor()
        cur.execute(f"DELETE FROM {table} WHERE ID = ?", (new_id,))
        con.commit()
        cur.close()
        print(f"  🧹 Компенсирующий DELETE выполнен")
    except Exception as e:
        print(f"  ⚠ Ошибка компенсирующего DELETE: {e}")


def send_and_record(local_path, remote_dir, master_id, who,
                    server=SERVER, share=SHARE, auth_file=None,
                    doc_base="", con=None, generator=None):
    """
    Отправка файла + запись в SCANNED с откатом при провале.

    Args:
        local_path:  локальный файл (расширение сохраняется в новом имени)
        remote_dir:  каталог на сервере (относительно share), напр. "FILES/22"
        master_id:   CARD.ID (MasterID)
        who:         аккаунт (WHO)
        doc_base:    префикс DOC_PATH:
                        ""     — remote_dir уже начинается от FILES,
                        "FILES"— корень share находится ВНУТРИ FILES
        con:         общее соединение fdb (для серии файлов — один раз снаружи)
        generator:   переопределить генератор (None = GEN_NAME / автопоиск)
    Возвращает записанный ID. При провале отправки — откат и повторный raise.
    """
    if not os.path.isfile(local_path):
        raise FileNotFoundError(f"Файл не найден: {local_path}")

    own_con = con is None
    if own_con:
        con = fdb.connect(
            host=DB_HOST, database=DB_DATABASE, user=DB_USER,
            password=DB_PASSWORD, fb_library_name=DB_LIBRARY,
            charset=DB_CHARSET,
        )

    try:
        gen = generator or GEN_NAME
        if not gen:
            gen, src = find_id_generator(con, TABLE)
            if not gen:
                raise RuntimeError(
                    f"Не удалось определить генератор ID для {TABLE}.\n"
                    f"Триггер: {src or '(не найден)'}\n"
                    "Укажите GEN_NAME вручную."
                )

        cur = con.cursor()

        # 1) Резервируем следующий ID (инкремент — внутри транзакции)
        cur.execute(f"SELECT GEN_ID({gen}, 1) FROM rdb$database")
        new_id = cur.fetchone()[0]
        print(f"  Зарезервирован ID={new_id}")

        # 2) Имя файла = "s" + ID + расширение исходника
        ext = os.path.splitext(local_path)[1]
        file_name = f"S{new_id}{ext}"
        remote_path = (remote_dir.strip("/") + "/" + file_name) if remote_dir else file_name
        doc_path = build_doc_path(remote_path, doc_base)

        # 3) Вставка с явным ID (пока НЕ зафиксирована)
        SAVEDATE =  datetime.now().strftime("%d.%m.%Y %H:%M")

        print(SAVEDATE)  # 24.11.2026
        # INSERT INTO SCANNED (ID, MASTERID, WHO, DOC_PATH, OLD_MASTERID, CARD_TYPE, SAVEDATE) VALUES (111, 178, 'trtrt', 'hghghghghg', 178, 0, '24.11.2026')
        cur.execute(
            f"INSERT INTO {TABLE} (ID, MasterID, WHO, DOC_PATH, OLD_MASTERID, CARD_TYPE, SAVEDATE) VALUES ( " + f"{new_id}, {master_id}, '{who}', '{doc_path}', {master_id}, 0, '{SAVEDATE}' )"
        )

        # 4) Отправка; при любой ошибке — откат записи и ID
        try:
            upload_via_smbclient(local_path, remote_path, server, share, auth_file)
        except Exception:
            _ensure_rolled_back(con, TABLE, new_id)
            cur.close()
            raise

        # 5) Успех — фиксация
        con.commit()
        cur.close()
        print(f"✅ Отправлено и записано в {TABLE}: ID={new_id}, "
              f"файл={file_name}, DOC_PATH={doc_path}")
        return new_id

    finally:
        if own_con:
            con.close()


# ─────────────────────────────────────────────────────
#  Запуск
# ─────────────────────────────────────────────────────
if __name__ == "__main__":

    # Один файл
    send_and_record(
        "/home/boss/Рабочий стол/разработка/card_reader/card_reader2/test.txt",
        f"FILES/{CARD_ID}",          # каталог на сервере
        master_id=CARD_ID,
        who=WHO,
    )

    # Серия файлов: одно соединение на весь цикл
    # con = fdb.connect(host=DB_HOST, database=DB_DATABASE, user=DB_USER,
    #                   password=DB_PASSWORD, fb_library_name=DB_LIBRARY,
    #                   charset=DB_CHARSET)
    # for rp in ["FILES/22/a.txt", "FILES/22/b.pdf"]:
    #     send_and_record(LOCAL_SRC, rp, master_id=CARD_ID, who=WHO, con=con)
    # con.close()