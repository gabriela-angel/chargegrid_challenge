"""O deslocamento de demanda do experimento COM x SEM tarifa dinâmica não pode criar nem destruir energia."""
import pytest

from scripts.experimento_tarifa import shift_energy


PRECOS = [1.4] * 6 + [1.9] * 6 + [2.4] * 6 + [1.9] * 6


@pytest.mark.parametrize("flex", [0.0, 0.2, 0.4])
def test_deslocamento_conserva_a_energia_total(flex):
    energia = [10.0 + h for h in range(24)]
    assert sum(shift_energy(energia, PRECOS, flex)) == pytest.approx(sum(energia))


def test_sem_flexiveis_nada_muda_e_com_flexiveis_o_pico_caro_esvazia():
    energia = [30.0] * 24
    assert shift_energy(energia, PRECOS, 0.0) == energia
    out = shift_energy(energia, PRECOS, 0.3)
    assert out[14] < energia[14]          # hora cara perde energia
    assert out[8] != energia[8] or out[3] > energia[3]
    assert all(e >= 0 for e in out)
