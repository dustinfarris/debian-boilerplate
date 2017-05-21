# debian-boilerplate

All new server setup automation with Fabric v2 (alpha) and Debian instead of Ubuntu.


## Installation

Clone this repo.

Then, assuming you already have virtualenv installed:

```
virtualenv env && source env/bin/activate
pip install -r requirements.txt
```


## Usage

Phoenix server:

```
fab -H 11.22.33.44 create_phoenix
```

Builder server:

```
fab -H 11.22.33.44 create_builder
```


## License

MIT
