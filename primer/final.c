#include <stdio.h>

int main()
{
    FILE *f_a = fopen("data/a.txt", "r");
    FILE *f_b = fopen("data/b.txt", "r");
    FILE *f_out = fopen("data/final.txt", "w");

    int num;
    int cntA = 0, sumA = 0;
    while (fscanf(f_a, "%d", &num) != EOF)
        sumA += num, cntA++;

    int cntB = 0, sumB = 0;
    while (fscanf(f_b, "%d", &num) != EOF)
        sumB += num, cntB++;

    fprintf(f_out, "Avg A: %f\nAvg B: %f\n", (double)sumA / cntA, (double)sumB / cntB);

    fclose(f_a);
    fclose(f_b);
    fclose(f_out);
    return 0;
}