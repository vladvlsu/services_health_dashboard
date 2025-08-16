**BASIC SERVICE HEATH UTILITY**

1. install dependencies:
`pip install -r requirements.txt`

2. run the service with:
`python3 health_map.py`

ALSO I WOULD RECOMMEND YOU TO ADD THE SERVICE TO YOUR LOCAL UTILS ON MAC:
1. `export PATH="$HOME/bin:$PATH"' >> ~/.zshrc`
2. `chmod -x health_map.py`
3. `cp health_map.py ~/usr/local/bin/health_map`

ADD NEW SERVICES:
in the find `sample_config` there, in json like format the services listed. set name, and url.
