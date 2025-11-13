
import sys

from .task_manager import action_from_args


def main():
    help_msg = 'Add one of the following commands and its arguments (`<command> -h` for help):\n'
    for action, description in action_from_args.actions.items():
        help_msg += '{}: {}\n'.format(action, description)

    try:
        command_or_input = sys.argv[1].lower()
    except IndexError:  # no command
        print(help_msg)
        exit()

    if command_or_input in ['-h', '--help']:
        print(help_msg)
        exit()

    sys.argv.pop(1)
    action_from_args(command_or_input)


if __name__ == '__main__':

    main()