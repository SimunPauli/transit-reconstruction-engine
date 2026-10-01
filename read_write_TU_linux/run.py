import subprocess
from pathlib import Path


TU_DATA_VERSION  = r"0625version2" # <--- pick TU dataversion
SECRET_DIR_PATH = r"/home/simpal/O/TU-Secret/DataVersions/" + TU_DATA_VERSION
CSV_OUTPUT_DIR = "/home/simpal/O/TU_Rejseplan/Data/TU/"


JAR_PATH = Path(__file__).parent / "target/TU-1.0-SNAPSHOT-jar-with-dependencies.jar"

matches = list(Path(SECRET_DIR_PATH).rglob("*.accdb"))
if len(matches) == 0:
    raise FileNotFoundError(f"No .accdb files found in {SECRET_DIR_PATH}")
if len(matches) > 1:
    raise ValueError(f"More than one .accdb file found: {matches}")
secret_db_path = matches[0]


def _read_write(table, db_path, csv_output_dir):
    csv_output_path = Path(csv_output_dir) / f"{table}.csv"
    subprocess.run(
        ["java", "-jar", str(JAR_PATH), str(db_path), str(csv_output_path), table],
        check=True,
    )


# write TU tables as csv
for table in ["dataset_session_secret_part","dataset_tur_secret_part", "dataset_tur", "dataset_deltur"]:
    _read_write(table, secret_db_path, CSV_OUTPUT_DIR)


