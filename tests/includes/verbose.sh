set_verbosity() {
	# There are three levels of verbosity, both 1 and 2 will fail on any error,
	# the difference is that 2 will also turn juju debug statements on, but not
	# the shell debug statements.
	case "${VERBOSE}" in
	1)
		set -eu
		;;
	2)
		set -eu
		;;
	11)
		# You asked for it!
		set -eux
		;;
	*)
		echo "Unexpected verbose level" >&2
		exit 1
		;;
	esac
}
