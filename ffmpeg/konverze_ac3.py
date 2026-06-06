import os
import subprocess

directory = r"."

# Bitrate výsledného AC3 audia
AC3_BITRATE = "640k"

def convert_to_ac3(mkv_path, out_path):
    cmd = [
        "ffmpeg", "-y",
        "-i", mkv_path,
        "-map", "0",
        "-c:v", "copy",
        "-c:a", "ac3",
        "-b:a", AC3_BITRATE,
        "-c:s", "copy",
        out_path
    ]
    subprocess.run(cmd)

def main():
    mkv_files = sorted(f for f in os.listdir(directory) if f.lower().endswith(".mkv"))

    if not mkv_files:
        print("Žádné MKV soubory nenalezeny.")
        return

    print(f"Nalezeno {len(mkv_files)} MKV souborů\n")
    print("=" * 50)

    done = 0
    errors = 0

    for filename in mkv_files:
        base = os.path.splitext(filename)[0]
        in_path  = os.path.join(directory, filename)
        out_path = os.path.join(directory, f"{base}_ac3.mkv")

        print(f"Zpracovávám: {filename}")
        print(f"  -> {os.path.basename(out_path)}")

        result = subprocess.run(
            ["ffmpeg", "-y", "-i", in_path,
             "-map", "0",
             "-c:v", "copy",
             "-c:a", "ac3", "-b:a", AC3_BITRATE,
             "-c:s", "copy",
             out_path],
            capture_output=False
        )

        if result.returncode == 0 and os.path.exists(out_path):
            size_mb = os.path.getsize(out_path) / (1024 * 1024)
            print(f"  [OK] Hotovo ({size_mb:.0f} MB)\n")
            done += 1
        else:
            print(f"  [CHYBA] Konverze selhala\n")
            errors += 1

    print("=" * 50)
    print(f"Dokončeno: {done}")
    print(f"Chyby:     {errors}")
    print("Hotovo!")

if __name__ == "__main__":
    main()
