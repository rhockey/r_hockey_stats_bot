from setuptools import setup, find_packages

setup(
    name="hockey_stats_rodriguez",
    description="Hockey Stats Bot",
    author="/u/pacefalmd",
    author_email="pacebots@gmail.com",
    version="1.0.4",
    packages=find_packages(),
    install_requires=[
        # Praw
        # Utilities
        # MAKE SURE TO INSTALL PRAW with PIP
        #"praw>4",
        "redis",
        "requests"
    ],
    classifiers=(
        "Development Status :: 5",
        "Fraomework :: Praw",
        "Programming Language :: Python :: 3"
    ),
    platforms="Python 3.4.3 and later."
)