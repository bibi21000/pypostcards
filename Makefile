#!/usr/bin/make -f
-include makefile.local

ifndef PYTHON
PYTHON:=python3
endif
VERSION := $(shell grep -m 1 version pyproject.toml | tr -s ' ' | tr -d '"' | tr -d "'" | cut -d' ' -f3)

.PHONY: venv build tests i18n-extract i18n-init i18n-update i18n-compile i18n

venv:
	${PYTHON} -m venv venv
	./venv/bin/pip install -e .
	./venv/bin/pip install -e .[dev]
	./venv/bin/pip install -e .[tkinter]
	./venv/bin/pip install -e .[flask]
	./venv/bin/pip install -e .[similar]
	./venv/bin/pip install -e .[scan]
	./venv/bin/pip install -e .[travel]

build:
	rm -rf dist build
	${MAKE} i18n-compile
	./venv/bin/python3 -m build

coverage:
	-./venv/bin/coverage combine
	./venv/bin/coverage report --include pypostcards

ruff:
	./venv/bin/ruff check src/

tests:
	./venv/bin/pytest  --random-order tests/

deps_scan:
	sudo apt-get install sane sane-utils

deps_ocr:
	sudo apt install tesseract-ocr tesseract-ocr-fra

i18n-extract:
	./venv/bin/python3 scripts/i18n.py extract

i18n-init:
	./venv/bin/python3 scripts/i18n.py init

i18n-update:
	./venv/bin/python3 scripts/i18n.py update

i18n-compile:
	./venv/bin/python3 scripts/i18n.py compile

i18n: i18n-update i18n-compile
