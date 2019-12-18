test:
	pytest -v test_swiftbackup.py

coverage:
	pytest --cov=swiftbackup --cov-branch --cov-report=term --cov-report=html \
		test_swiftbackup.py

.PHONY: test coverage
