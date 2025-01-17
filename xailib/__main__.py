import argparse
import subprocess

parser = argparse.ArgumentParser()
parser.add_argument(
    "--concept-score", action="store_true", help="Run the concept score script"
)
parser.add_argument(
    "--tcav-score", action="store_true", help="Run the tcav score script"
)
parser.add_argument(
    "--concept-backprop",
    action="store_true",
    help="Run the concept backpropagation script",
)
args = parser.parse_args()

if args.concept_score:
    subprocess.run(["python", "xailib/scripts/concept_score.py"])
if args.tcav_score:
    subprocess.run(["python", "xailib/scripts/tcav.py"])
if args.concept_backprop:
    subprocess.run(["python", "xailib/scripts/concept_backpropagation.py"])
