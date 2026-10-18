def formatteer_huisnummers(huisnummers):
    try:
        # Zet om naar integers, sorteer en haal duplicaten weg
        num_ints = sorted(list({int(h) for h in huisnummers if str(h).isdigit()}))

        if len(num_ints) > 2:
            # Bereken de verschillen tussen opeenvolgende nummers
            diffs = [num_ints[i+1] - num_ints[i] for i in range(len(num_ints)-1)]

            # Als alle stappen gelijk zijn aan 1 (normaal) OF 2 (even/oneven)
            if len(set(diffs)) == 1 and diffs[0] in (1, 2):
                return f"{num_ints[0]}-{num_ints[-1]}"

    except ValueError:
        pass

    # Fallback naar de standaard lijst als het geen strakke reeks is
    return "; ".join(huisnummers)
