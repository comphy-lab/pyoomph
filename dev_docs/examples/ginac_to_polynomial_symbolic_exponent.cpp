// GiNaC: ex::to_polynomial() recurses forever on a power with a NEGATIVE INTEGER exponent whose
// basis is itself a power with a SYMBOLIC exponent, e.g. (x^a)^(-2). The program below runs out of
// stack in a few tens of thousands of frames of GiNaC::power::to_polynomial.
//
// Seen on 1.8.10 built from source. No pyoomph involved; this is what to send upstream.
//
// ginac/normal.cpp, power::to_polynomial:
//
//     else if (exponent.info(info_flags::negint))
//     {
//         ex basis_pref = collect_common_factors(basis);
//         if (is_exactly_a<mul>(basis_pref) || is_exactly_a<power>(basis_pref)) {
//             // (A*B)^n will be automagically transformed to A^n*B^n
//             ex t = pow(basis_pref, exponent);
//             return t.to_polynomial(repl);
//         }
//         else
//             return pow(replace_with_symbol(pow(basis, _ex_1), repl), -exponent);
//     }
//
// The comment justifies the recursion: rebuilding pow(basis_pref, exponent) is expected to make
// progress because GiNaC flattens the result. It does for a `mul` basis. It does NOT for a `power`
// basis with a symbolic exponent: power::eval only contracts (x^a)^b into x^(a*b) when that is
// provably valid (b an integer AND a real, or a itself an integer), because for symbolic a the two
// differ on the branch cut. So pow((x^a), -2) evaluates back to the very expression to_polynomial
// was called on, and the "recursion" is a self-call with an unchanged argument.
//
// The other branches all terminate: a posint exponent descends into the basis, and the else branch
// replaces the whole power by a fresh symbol.
//
// The one-line fix is to recurse only when the rebuilt expression actually differs, and to fall
// through to the replace_with_symbol branch when it does not:
//
//         if (is_exactly_a<mul>(basis_pref) || is_exactly_a<power>(basis_pref)) {
//             ex t = pow(basis_pref, exponent);
//             if (!t.is_equal(*this))
//                 return t.to_polynomial(repl);
//         }
//         return pow(replace_with_symbol(pow(basis, _ex_1), repl), -exponent);
//
// which leaves every terminating case bit-identical (there t differs from *this by construction)
// and turns the non-terminating one into the opaque-symbol replacement that to_polynomial applies
// to any other non-polynomial subexpression, e.g. the (x^a)^(1/2) case below already.
//
// How pyoomph hits it: a Maxwell-Stefan diffusivity interpolated by the Vignes rule is
// D0_ij^e_ij * D0_ji^e_ji with the exponents e depending on the composition FIELDS. When the
// mixture viscosity inside D0 is a sum (a weighted average over the components), the unit symbols
// do not cancel automatically, so the expression retains factors like kilogram^(<field expr>).
// pyoomph's unit extraction calls collect_common_factors on that, which calls to_polynomial, and
// the process dies with no diagnostic. See pyoomph/materials/diffusivity_estimates.py, which builds
// the interpolation as exp(e*log(D0)) instead.
//
// Build (with $P the third-party install prefix, e.g. build/thirdparty-install):
//     g++ -O0 -g -o ginac_to_polynomial_symbolic_exponent ginac_to_polynomial_symbolic_exponent.cpp \
//         -I$P/include -L$P/lib -lginac -lcln -lgmp
// Exits 1 (and prints which case blew the stack) while the bug is present, 0 when it is fixed.

#include <csetjmp>
#include <csignal>
#include <cstdlib>
#include <iostream>
#include <ginac/ginac.h>

using namespace GiNaC;

static sigjmp_buf jump_target;
static void on_stack_overflow(int) { siglongjmp(jump_target, 1); }

// Runs e.to_polynomial() and returns false if it overflowed the stack. The handler needs its own
// stack, since the thread stack is exactly what is exhausted when it fires.
static bool to_polynomial_terminates(const ex &e, ex &result)
{
	static char handler_stack[256 * 1024];  // SIGSTKSZ is not a constant on glibc 2.34+
	stack_t ss;
	ss.ss_sp = handler_stack;
	ss.ss_size = sizeof(handler_stack);
	ss.ss_flags = 0;
	sigaltstack(&ss, nullptr);

	struct sigaction sa;
	sa.sa_handler = on_stack_overflow;
	sigemptyset(&sa.sa_mask);
	sa.sa_flags = SA_ONSTACK | SA_NODEFER | SA_RESETHAND;
	sigaction(SIGSEGV, &sa, nullptr);

	if (sigsetjmp(jump_target, 1) != 0)
		return false;
	exmap repl;
	result = e.to_polynomial(repl);
	return true;
}

int main()
{
	symbol x("x"), a("a");

	struct Case { const char *name; ex e; };
	const Case cases[] = {
		{"(x^a)^(-2)   power basis, symbolic exponent, negint outer", pow(pow(x, a), -2)},
		{"(x^a)^(1/2)  the same basis under a non-integer exponent", pow(pow(x, a), numeric(1, 2))},
		{"(x*a)^(-2)   mul basis, which really does flatten", pow(x * a, -2)},
		{"(x^3)^(-2)   numeric inner exponent, which also flattens", pow(pow(x, 3), -2)},
		{"(x+a)^(-2)   plain add basis", pow(x + a, -2)},
	};

	int failures = 0;
	for (const Case &c : cases) {
		// pow() evaluates on construction; print what GiNaC actually built, since the whole
		// question is whether it contracted the double power or not.
		std::cout << c.name << "\n    built as: " << c.e << "\n    to_polynomial: " << std::flush;
		ex result;
		if (to_polynomial_terminates(c.e, result)) {
			std::cout << result << "\n";
		} else {
			std::cout << "*** STACK OVERFLOW (infinite recursion) ***\n";
			++failures;
		}
	}
	if (failures) {
		std::cout << "\n" << failures << " case(s) recursed forever: the bug is present.\n";
		return 1;
	}
	std::cout << "\nAll cases terminated: the bug is fixed.\n";
	return 0;
}
