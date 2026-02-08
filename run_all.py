from portals import olx, otomoto, autoscout24, mobilede
from utils import seen, save_seen, load_seen

seen = load_seen()

def safe_run(func, name):
    try:
        func.run(seen)
    except Exception as e:
        print(f"{name} failed: {e}")

def main():
    safe_run(olx, "OLX")
    safe_run(otomoto, "Otomoto")
    safe_run(autoscout24, "AutoScout24")
    safe_run(mobilede, "Mobile.de")
    save_seen(seen)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"Unexpected global error: {e}")
