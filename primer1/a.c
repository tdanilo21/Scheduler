#include <stdio.h>
#include <stdlib.h>
#include <time.h>

int main()
{
    srand((unsigned int)time(NULL));

    FILE *f = fopen("data/a.txt", "w");

    int n = rand() % 1000 + 1;
    for (int i = 0; i < n; i++)
        fprintf(f, "%d ", rand() % 1000);
    fprintf(f, "\n");

    fclose(f);
    return 0;
}