#!/usr/bin/env python3

"""pgen.py - Generate a parser from a pgen grammar.

(This is for testing. pgen will likely go away. Ignore this for now.)
"""

import argparse
import parse_pgen
import gen
import sys


def main():
    parser = argparse.ArgumentParser(description="Generate a parser.")
    parser.add_argument('--target', choices=['python', 'rust'], default='rust',
                        help="target language to use when printing the parser tables")
    parser.add_argument('grammar', metavar='FILE', nargs=1,
                        help=".pgen file containing the grammar")
    options = parser.parse_args()

    [pgen_filename] = options.grammar
    grammar, goal_nts = parse_pgen.load_grammar(pgen_filename)
    gen.generate_parser(sys.stdout, grammar, goal_nts, target=options.target)


if __name__ == '__main__':
    main()
