import json

with open("iobs_test_config_old.json", "r") as f:
    config = json.load(f)
    
result = []
for item in config:
    result.append({
        "name": item["name"],
        "description": item["description"],
        "checker_args": {
            "--probability_tolerance": 0.1,
        },
        "cases": [{
            "sim_stdin_file": case['sim_stdin_file'],
            "num": 20,
            "sim_args": {
                "--simulation_time": 1000000.0
            },
            "checker_config": {
                **item['checker_args']
            }
        } for case in item["cases"]]
    })
    
with open("iobs_test_config.json", "w") as f:
    json.dump(result, f, indent=2, ensure_ascii=False)